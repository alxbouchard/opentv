"""Source de lecture « Regarder » : configuration hors Git, porte d'autorisation fail-closed, résolveur d'adresse.

Contrat (identique au pont NanoPi, sans le copier) :
- `/etc/opentv/source.env` (root:opentv 0640, jamais dans Git ni dans le manifeste) porte la racine de confiance stable
  et l'approbation explicite `OPENTV_SOURCE_APPROVED=1`. Sans ces deux éléments, fichier absent/peu sûr/invalide :
  `available` est faux, « Regarder » n'apparaît pas et aucune requête n'est émise.
- Le résolveur d'adresse changeante et la table de routage `site_id -> fiche` sont ceux du NanoPi
  (`opentv_bridge.Resolver` / `RouteMap`), chargés par chemin depuis le moteur installé et inchangés (empreinte
  vérifiée par le manifeste). L'époque {origine, préfixe, jeton} est validée et mise en cache comme un tout.
- Aucun domaine, préfixe, jeton ni URL n'est journalisé : le journal du moteur passe par `redact_fields`.
- Aucune URL ne sort de ce module vers la TV ou les agents : `detail_url` sert uniquement au contrôleur du lecteur
  (socket Unix authentifié). Aucun flux n'est extrait, reconstruit ni relayé.

Mode « flux officiel » (`OPENTV_SOURCE_KIND=official-stream`) : réservé à une API ou à un point HLS/DASH documenté et
fourni par l'ayant droit. Aucun n'est configuré ni découvert à l'exécution ; ce mode est donc refusé
(`direct_mode_unconfigured`) et seul le lecteur du site (`site-player`) peut fonctionner.
"""
import importlib.util
import os
import re
import stat
import time
import urllib.parse

SOURCE_FILE = "/etc/opentv/source.env"
KINDS = ("site-player", "official-stream")
KEY_RE = re.compile(r"OPENTV_[A-Z0-9_]{1,40}")
DEFAULTS = {
    "OPENTV_SOURCE_APPROVED": "0",
    "OPENTV_SOURCE_KIND": "site-player",
    "OPENTV_SOURCE_TRUST_ANCHOR": "",
    "OPENTV_PLAYER_MAX_S": "14400",
    "OPENTV_PLAY_ASSIST_S": "15",
    "OPENTV_PLAY_SETTLE_S": "1.5",
    "OPENTV_SOURCE_FETCH_TIMEOUT_S": "4",
    "OPENTV_SOURCE_SESSION_CHECK_S": "300",
    "OPENTV_ROUTE_BUDGET_S": "6",
}
# champs du journal du moteur admis tels quels : jamais d'hôte, de préfixe, de jeton, d'époque ni d'URL
SAFE_LOG_FIELDS = ("changed", "migrated", "pages", "step", "transient", "error", "outcome")
SAFE_WHY_RE = re.compile(r"^[a-z_:0-9]{1,48}$")


class SourceError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def redact_fields(fields):
    """Champs de journal sûrs seulement (liste blanche) ; « why » gardé s'il ressemble à un code d'erreur."""
    out = {k: fields[k] for k in SAFE_LOG_FIELDS if k in fields and isinstance(fields[k], (bool, int, float, str))
           and not (isinstance(fields[k], str) and ("." in fields[k] or "/" in fields[k]))}
    why = fields.get("why")
    if isinstance(why, str) and SAFE_WHY_RE.match(why):
        out["why"] = why
    return out


def _read_private(path, cap=8192, uid_ok=(0,), gid_ok=None):
    """Lecture sans suivre de lien : fichier régulier, nlink 1, propriétaire admis, aucun accès « autres »."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise SourceError("missing")
    except OSError:
        raise SourceError("unsafe")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o007 or st.st_mode & 0o022 \
                or st.st_uid not in uid_ok or st.st_size > cap:
            raise SourceError("unsafe")
        if gid_ok is not None and st.st_gid not in gid_ok and st.st_uid != os.geteuid():
            raise SourceError("unsafe")
        data = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise SourceError("unsafe")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceError("invalid")


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if KEY_RE.fullmatch(k):
            out[k] = v
    return out


def _num(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SourceError("invalid")
    if not lo <= f <= hi:
        raise SourceError("invalid")
    return f


class SourceConfig:
    """État de la porte : `status` ∈ {ok, missing, unsafe, invalid, unapproved, no_anchor, direct_mode_unconfigured}."""

    def __init__(self, status, values=None):
        self.status = status
        v = dict(DEFAULTS)
        v.update(values or {})
        self.kind = v["OPENTV_SOURCE_KIND"] if v["OPENTV_SOURCE_KIND"] in KINDS else "site-player"
        self.anchor = v["OPENTV_SOURCE_TRUST_ANCHOR"] if status == "ok" else ""
        self.max_s = self.assist_s = self.settle_s = 0.0
        self.fetch_timeout_s, self.session_check_s, self.route_budget_s = 4.0, 300.0, 6.0
        if status == "ok":
            self.max_s = _num(v["OPENTV_PLAYER_MAX_S"], 60, 6 * 3600)
            self.assist_s = _num(v["OPENTV_PLAY_ASSIST_S"], 3, 60)
            self.settle_s = _num(v["OPENTV_PLAY_SETTLE_S"], 0, 10)
            self.fetch_timeout_s = _num(v["OPENTV_SOURCE_FETCH_TIMEOUT_S"], 1, 15)
            self.session_check_s = _num(v["OPENTV_SOURCE_SESSION_CHECK_S"], 30, 86400)
            self.route_budget_s = _num(v["OPENTV_ROUTE_BUDGET_S"], 1, 30)

    @property
    def ok(self):
        return self.status == "ok"

    @classmethod
    def load(cls, path=SOURCE_FILE, uid_ok=(0,), gid_ok=None, valid_host=None):
        try:
            values = parse_env(_read_private(path, uid_ok=uid_ok, gid_ok=gid_ok))
        except SourceError as e:
            return cls(e.code)
        if values.get("OPENTV_SOURCE_APPROVED") != "1":
            return cls("unapproved")
        kind = values.get("OPENTV_SOURCE_KIND", "site-player")
        if kind not in KINDS:
            return cls("invalid")
        if kind == "official-stream":
            # aucune API/point HLS-DASH documenté par un ayant droit n'est pris en charge : jamais de découverte
            return cls("direct_mode_unconfigured")
        anchor = values.get("OPENTV_SOURCE_TRUST_ANCHOR", "")
        if not anchor_ok(anchor, valid_host):
            return cls("no_anchor")
        try:
            return cls("ok", values)
        except SourceError as e:
            return cls(e.code)


def anchor_ok(anchor, valid_host=None):
    """https://hôte/ exactement : ni identifiants, ni port, ni chemin, ni requête ; nom public (règles du NanoPi)."""
    if not isinstance(anchor, str) or len(anchor) > 300:
        return False
    u = urllib.parse.urlsplit(anchor)
    host = (u.hostname or "").lower()
    try:
        port = u.port
    except ValueError:
        return False
    if u.scheme != "https" or u.username or u.password or port not in (None, 443) or u.path not in ("", "/") \
            or u.query or u.fragment or not host or u.netloc.lower() != host:
        return False
    return valid_host(host) if valid_host else bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,63}$", host))


def load_engine(engine_dir, name="opentv_pi_source_engine"):
    path = os.path.join(engine_dir, "opentv_bridge.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SourceError("engine_missing")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except (OSError, ImportError, SyntaxError):
        raise SourceError("engine_missing")
    for attr in ("Resolver", "RouteMap", "DiscoveryFailed", "load_catalogue_meta", "valid_public_host", "Config"):
        if not hasattr(mod, attr):
            raise SourceError("engine_incompatible")
    return mod


class Source:
    """Résolution « élément du catalogue -> fiche validée » pour le contrôleur du lecteur (jamais pour la TV)."""

    def __init__(self, cfg, engine_dir, state_dir, catalogue_file, log=None, fetch=None, clock=time.time,
                 engine=None):
        self.cfg, self.catalogue_file, self.log = cfg, catalogue_file, log or (lambda *a, **k: None)
        self.br = engine or load_engine(engine_dir)
        # journal du moteur redirigé et caviardé (le moteur journalise hôte/préfixe/jeton en clair sur le NanoPi)
        self.br.log = lambda event, **f: self.log("source_" + str(event)[:40], **redact_fields(f))
        self.detail_re = self.br.Config({}).detail_re
        self.resolver = self.router = None
        if cfg.ok:
            os.makedirs(state_dir, mode=0o700, exist_ok=True)
            self.resolver = self.br.Resolver(cfg.anchor, os.path.join(state_dir, "prefix.json"), fetch=fetch,
                                             clock=clock, timeout=cfg.fetch_timeout_s,
                                             session_check=cfg.session_check_s)
            self.router = self.br.RouteMap(self.resolver, os.path.join(state_dir, "routes.json"))

    @property
    def available(self):
        return self.resolver is not None

    def item(self, uid_item):
        """Élément validé du catalogue accepté (clean_item du NanoPi) correspondant à l'élément « Pour toi »."""
        items, _, host = self.br.load_catalogue_meta(self.catalogue_file, self.detail_re)
        want_sid = str(uid_item.get("site_id") or "")
        want_path = uid_item.get("detail_path")
        for it in items:
            if (want_sid and it.get("site_id") == want_sid) or (not want_sid and it.get("detail_path") == want_path):
                return it, host
        raise SourceError("not_in_catalogue")

    def _direct_ok(self, item, ep, host):
        host = host or self.resolver.anchor
        return bool(ep) and host == ep["host"] and item["detail_path"].split("/")[1] == ep.get("token")

    def detail_url(self, uid_item, deadline):
        """-> (url, origine) de la fiche, époque validée + chemin exact (catalogue même époque ou table de routage
        publiée par le site). Jamais d'identifiant calculé ni d'URL fournie par un appelant. Lève SourceError."""
        if not self.available:
            raise SourceError("unavailable")
        item, host = self.item(uid_item)
        D = self.br.DiscoveryFailed
        try:
            self.resolver.current(budget=max(0.5, deadline - time.monotonic()), check=True)
        except D:
            raise SourceError("discovery_failed")
        ep = self.resolver.epoch()
        if not ep or not ep.get("token"):
            raise SourceError("no_epoch")
        origin, prefix, token = ep["origin"], ep["prefix"], ep["token"]
        direct, sid, rel = self._direct_ok(item, ep, host), item.get("site_id"), None
        if sid:
            kind, found = (self.router.peek(ep, sid) if direct else
                           self.router.resolve(ep, sid, min(deadline, time.monotonic() + self.cfg.route_budget_s)))
            if kind == "found":
                rel = found
            elif kind == "absent" and not direct:
                raise SourceError("unmapped")
        if rel is None:
            if not direct:
                raise SourceError("unmapped")
            rel = item["detail_path"]
        url = "%s/%s/%s" % (origin, prefix, rel)
        u = urllib.parse.urlsplit(url)
        tail = u.path[len(prefix) + 2:]
        if (u.scheme + "://" + u.netloc) != origin or not u.path.startswith("/%s/" % prefix) \
                or not self.detail_re.match(tail) or not re.match(r"^b/%s/[0-9]{1,12}$" % re.escape(token), tail) \
                or u.query or u.fragment:
            raise SourceError("bad_url")
        return url, origin
