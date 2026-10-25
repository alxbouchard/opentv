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
  /<préfixe>/ + chemin de fiche validé (DETAIL_RE). Aucun flux vidéo n'est extrait, relayé ou contourné.
"""
import html.parser
import http.server
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
        self.detail_re = re.compile(env.get("DETAIL_RE", r"^b/fakrov/[0-9]{1,12}$"))
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


# --------------------------------------------------------------------------- préfixe rotatif
class DiscoveryFailed(Exception):
    pass


class _Links(html.parser.HTMLParser):
    def __init__(self, cap=5000):
        super().__init__(convert_charrefs=True)
        self.hrefs, self.cap = [], cap

    def handle_starttag(self, tag, attrs):
        if tag == "a" and len(self.hrefs) < self.cap:
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v.strip())


class Resolver:
    """Découvre et valide https://<hôte>/<préfixe>/ à partir d'une racine stable."""

    def __init__(self, root, cache_path, fetch=None, clock=time.time, ttl=6 * 3600, grace=24 * 3600,
                 timeout=4.0):
        u = urllib.parse.urlsplit(root)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.port not in (None, 443):
            raise ValueError("SITE_ROOT doit être https://hôte/ sans identifiants ni port")
        self.host = u.hostname.lower()
        self.origin = "https://" + self.host
        self.root = self.origin + "/"
        self.cache_path, self.fetch = cache_path, fetch or http_fetch
        self.clock, self.ttl, self.grace, self.timeout = clock, ttl, grace, timeout
        self.lock = threading.Lock()
        self.cache = self._load()

    # -- cache 0600 {prefix, discovered_at, validated_at, listing_path}
    def _load(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                c = json.load(f)
            if PREFIX_RE.match(c.get("prefix", "")) and float(c["validated_at"]) > 0:
                return c
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def _save(self, c):
        atomic_write_json(self.cache_path, c)

    def same_origin(self, url):
        """URL absolue acceptée (http même hôte remonté en https) ou None."""
        u = urllib.parse.urlsplit(url)
        if u.username or u.password or (u.hostname or "").lower() != self.host:
            return None
        if u.scheme == "http" and u.port in (None, 80):
            pass
        elif u.scheme != "https" or u.port not in (None, 443):
            return None
        return urllib.parse.urlunsplit(("https", self.host, u.path or "/", u.query, ""))

    def _get(self, url, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise DiscoveryFailed("timeout")
        try:
            return self.fetch(url, min(self.timeout, left))
        except FetchError as e:
            raise DiscoveryFailed("fetch:" + str(e))

    def _follow(self, url, deadline, must_start=None):
        """Suit ≤3 redirections même origine (et sous must_start si donné) ; exige 200 text/html."""
        for _ in range(4):
            status, headers, body = self._get(url, deadline)
            if status in (301, 302, 303, 307, 308):
                nxt = self.same_origin(urllib.parse.urljoin(url, headers.get("location", "")))
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

    def validate(self, prefix, deadline):
        start = "/" + prefix + "/"
        final, body = self._follow(self.origin + start, deadline, must_start=start)
        return urllib.parse.urlsplit(final).path, body

    def discover(self, deadline):
        final, body = self._follow(self.root, deadline)
        path = urllib.parse.urlsplit(final).path.strip("/").split("/")
        cands = [path[0]] if path and PREFIX_RE.match(path[0]) else []
        p = _Links()
        try:
            p.feed(body.decode("utf-8", "replace"))
        except Exception:
            pass
        for href in p.hrefs:
            absu = self.same_origin(urllib.parse.urljoin(final, href))
            if not absu:
                continue
            segs = [s for s in urllib.parse.urlsplit(absu).path.split("/") if s]
            if len(segs) == 1 and PREFIX_RE.match(segs[0]) and segs[0] not in cands:
                cands.append(segs[0])
        if not cands:
            raise DiscoveryFailed("no_prefix_link")
        last = "no_candidate"
        for c in cands[:3]:   # premier candidat valide ; borne à 3 sondes
            try:
                listing, _ = self.validate(c, deadline)
                return c, listing
            except DiscoveryFailed as e:
                last = str(e)
        raise DiscoveryFailed(last)

    def refresh(self, budget=None):
        """Valide le préfixe en cache, sinon une seule redécouverte. Lève DiscoveryFailed."""
        deadline = time.monotonic() + (budget if budget else 3 * self.timeout + 2)
        with self.lock:
            c = self.cache
            now = self.clock()
            if c:
                try:
                    listing, _ = self.validate(c["prefix"], deadline)
                    c = dict(c, validated_at=now, listing_path=listing)
                    self.cache = c
                    self._save(c)
                    return c
                except DiscoveryFailed as e:
                    log("prefix_invalid", prefix=c["prefix"], why=str(e))
            prefix, listing = self.discover(deadline)
            changed = not c or c.get("prefix") != prefix
            c = {"prefix": prefix, "discovered_at": now if changed else c.get("discovered_at", now),
                 "validated_at": now, "listing_path": listing}
            self.cache = c
            self._save(c)
            log("prefix_ok", prefix=prefix, changed=changed)
            return c

    def status(self):
        c = self.cache
        if not c:
            return "none"
        age = self.clock() - float(c["validated_at"])
        return "fresh" if age < self.ttl else ("stale" if age < self.grace else "expired")

    def current(self, budget=None):
        """(origine, préfixe, 'fresh'|'stale'). Réseau seulement si aucun préfixe utilisable."""
        st = self.status()
        if st in ("none", "expired"):
            try:
                self.refresh(budget)
            except DiscoveryFailed as e:
                log("discovery_failed", why=str(e))
                raise
            st = "fresh"
        return self.origin, self.cache["prefix"], st


# --------------------------------------------------------------------------- catalogue (métadonnées)
class _Cards(html.parser.HTMLParser):
    """Lit les cartes <a href="/<p>/b/fakrov/<id>"> : titre (alt/titre), affiche https, année, genre."""

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
            self.cur = {"href": a["href"], "title": "", "poster": "", "year": "", "cat": ""}
            self.items.append(self.cur)
            self.stack = []
        elif self.cur is not None:
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
            "source": resolver.host, "source_id": segs[-1], "detail_path": rel,
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
    return {
        "source_id": str(x.get("source_id") or rel.split("/")[-1])[:64], "detail_path": rel,
        "title": x["title"].strip()[:200], "year": num("year", 1880, 2100, int), "genre": s("genre", 80),
        "description": s("description", 1200),
        "poster": poster if isinstance(poster, str) and poster.startswith("https://") and len(poster) < 600 else None,
        "imdb_id": x["imdb_id"] if isinstance(x.get("imdb_id"), str) and re.match(r"^tt\d{5,10}$", x["imdb_id"]) else None,
        "imdb_rating": num("imdb_rating", 0, 10, (int, float)), "imdb_votes": num("imdb_votes", 0, 10 ** 9, int),
        "match_status": s("match_status", 16), "rating_updated_at": s("rating_updated_at", 40),
    }


def load_catalogue(path, detail_re, cap=2000):
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return [], None
    items = raw.get("items") if isinstance(raw, dict) else raw
    out, seen = [], set()
    for x in (items if isinstance(items, list) else [])[:cap * 2]:
        c = clean_item(x, detail_re)
        if c and c["detail_path"] not in seen:
            seen.add(c["detail_path"])
            out.append(c)
    return out[:cap], (raw.get("generated_at") if isinstance(raw, dict) else None)


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


class Controller:
    def __init__(self, cfg, resolver, display_factory, clock=time.monotonic):
        self.cfg, self.resolver, self.factory, self.clock = cfg, resolver, display_factory, clock
        self.lock = threading.RLock()
        self.td_cv, self.td_n = threading.Condition(), 0   # fermetures en cours (pas d'ouverture pendant)
        self.s = None
        self.items, self.generated_at = load_catalogue(cfg.catalogue_file, cfg.detail_re)
        self.message = ""

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
            self.items, self.generated_at = load_catalogue(self.cfg.catalogue_file, self.cfg.detail_re)
            now = self.clock()
            s = {"id": secrets.token_urlsafe(18), "app": app, "state": "starting", "cursor": 0,
                 "opened": now, "seen": now, "key_at": now, "player_at": None, "display": None}
            self.s, self.message = s, ""
            log("session_open", app=app, session=s["id"][:6], items=len(self.items), prefix=self.resolver.status())
        threading.Thread(target=self._start, args=(s,), daemon=True, name="display-start").start()
        return s

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
                return self._open_item(s) if st == "catalogue" else st
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

    def detail_url(self, item):
        origin, prefix, _ = self.resolver.current(budget=3.0)
        url = "%s/%s/%s" % (origin, prefix, item["detail_path"])
        # revérification indépendante : même origine, sous /<préfixe>/, chemin de fiche autorisé
        u = urllib.parse.urlsplit(url)
        if (u.scheme + "://" + u.netloc) != origin or not u.path.startswith("/%s/" % prefix) \
                or not self.cfg.detail_re.match(u.path[len(prefix) + 2:]) or u.query or u.fragment:
            raise ApiError(500, "bad_url")
        return url

    def _open_item(self, s):
        if not self.items:
            return s["state"]
        item = self.items[min(s["cursor"], len(self.items) - 1)]
        try:
            url = self.detail_url(item)
            s["display"].navigate(url)
        except (DiscoveryFailed, ApiError, CDPError, AttributeError) as e:
            self.message = "Page indisponible"
            log("player_open_failed", item=item["source_id"], error=str(e)[:80])
            self._push(s)
            return s["state"]
        s["state"], s["player_at"], self.message = "player", self.clock(), ""
        log("state", state="player", item=item["source_id"])
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


# --------------------------------------------------------------------------- catalogue en tâche de fond
def refresh_catalogue(cfg, resolver):
    """Rafraîchit préfixe + catalogue (page de liste) ; garde l'ancien fichier en cas d'échec."""
    c = resolver.refresh()
    url = resolver.origin + c["listing_path"]
    status, headers, body = resolver.fetch(url, cfg.fetch_timeout_s * 2)
    if status != 200 or not headers.get("content-type", "").startswith("text/html"):
        raise DiscoveryFailed("listing_status_%d" % status)
    items = parse_listing(body.decode("utf-8", "replace"), url, resolver, cfg.detail_re)
    if not items:
        raise DiscoveryFailed("listing_empty")
    old, _ = load_catalogue(cfg.catalogue_file, cfg.detail_re)
    prev = {x["detail_path"]: x for x in old}
    for it in items:   # conserve les enrichissements (description, IMDb) déjà connus
        for k, v in prev.get(it["detail_path"], {}).items():
            if it.get(k) is None and v is not None:
                it[k] = v
    atomic_write_json(cfg.catalogue_file, {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           "source": resolver.host, "count": len(items), "items": items})
    log("catalogue_ok", count=len(items))
    return items


def serve(cfg):
    os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    resolver = Resolver(cfg.site_root, os.path.join(cfg.state_dir, "prefix.json"), ttl=cfg.prefix_ttl_s,
                        grace=cfg.prefix_grace_s, timeout=cfg.fetch_timeout_s)
    factory = {"chromium": ChromiumKiosk, "null": NullDisplay, "off": None}[cfg.display]
    ctl = Controller(cfg, resolver, factory)
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
                        ctl.items, ctl.generated_at = load_catalogue(cfg.catalogue_file, cfg.detail_re)
                wait = cfg.catalogue_refresh_s
            except (DiscoveryFailed, FetchError, OSError) as e:
                log("catalogue_refresh_failed", error=str(e)[:80])
                wait = 600
            stop.wait(wait)

    def on_term(*_):
        stop.set()
        threading.Thread(target=api.shutdown, daemon=True).start()
        threading.Thread(target=ui.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    for fn in (watchdog, ui.serve_forever) + ((refresher,) if cfg.source_approved and cfg.catalogue_refresh_s > 0 else ()):
        threading.Thread(target=fn, daemon=True).start()
    log("start", port=cfg.port, ui_port=cfg.ui_port, display=cfg.display, allow=sorted(cfg.allow_ips))
    api.serve_forever(poll_interval=0.5)
    ctl.shutdown()
    log("stop")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    cfg = Config()
    if cmd == "serve":
        serve(cfg)
    elif cmd == "refresh-catalogue":
        if not cfg.source_approved:
            raise SystemExit("source non approuvée: définir SOURCE_APPROVED=1 après validation humaine")
        r = Resolver(cfg.site_root, os.path.join(cfg.state_dir, "prefix.json"), ttl=cfg.prefix_ttl_s,
                     grace=cfg.prefix_grace_s, timeout=cfg.fetch_timeout_s)
        print(len(refresh_catalogue(cfg, r)))
    else:
        raise SystemExit("usage: opentv_bridge.py [serve|refresh-catalogue]")


if __name__ == "__main__":
    main(sys.argv)
