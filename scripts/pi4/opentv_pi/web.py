"""Serveurs HTTP du service (stdlib).

- TV (127.0.0.1 seulement, toujours) : /tv, /static/*, /api/tv/* (en-tête secret par démarrage, Host vérifié contre le
  DNS rebinding, JSON borné). CSP stricte : aucun script en ligne, connexions vers soi uniquement.
- LAN (0.0.0.0:OPENTV_SETUP_PORT, seulement pendant la configuration) : /setup (assistant en 3 étapes, utilisable
  sans JavaScript), /static/setup.css et /static/setup.js. Rien d'autre. Le jeton n'apparaît jamais dans un journal.
- Gestion des assistants (Réglages TV, configuré et en ligne seulement) : même port, mais écoute sur l'adresse LAN
  actuelle (jamais 0.0.0.0) et **seulement** /assistants + /static/setup.css (aucun /setup, aucun balayage Wi-Fi) ;
  jeton distinct à usage unique, Host = adresse liée, pairs IPv4 privés, Origin exacte + jeton CSRF au POST,
  Referrer-Policy same-origin, aucun script.
- Télécommande sur téléphone (<adresse LAN actuelle>:OPENTV_REMOTE_PORT, jamais 0.0.0.0, seulement configuré et en
  ligne ; écoute relancée si l'adresse change) : /r (appairage par QR seulement, puis pavé de navigation), POST
  /r/pair, /r/cmd, /r/forget ; /static/remote.css|js. Host = adresse liée ou nom mDNS exact de la boîte (avec le
  port) ; pairs IPv4 privés hors boucle locale ; pages en Referrer-Policy same-origin (le navigateur envoie alors
  l'Origin réelle des formulaires ; « Origin: null » est toujours refusé). HTTP sur le LAN : prototype (aucun
  certificat propre à l'appareil).
"""
import base64
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse

from . import VERSION, bounded, phoneremote, validate, wifiscan
from . import app as appmod   # constantes d'erreur du formulaire (app n'importe pas web : pas de cycle)

TV_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
          "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# Page du téléphone : un seul script externe de même origine (afficher le mot de passe, double envoi), aucun script
# en ligne, aucune connexion (connect-src hérite de default-src 'none'), formulaires vers soi uniquement.
SETUP_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; "
             "base-uri 'none'; frame-ancestors 'none'")
TV_STATIC = {"tv.js": "application/javascript; charset=utf-8", "keys.js": "application/javascript; charset=utf-8",
             "tv.css": "text/css; charset=utf-8",
             "qrcode.js": "application/javascript; charset=utf-8", "setup.css": "text/css; charset=utf-8"}
SETUP_STATIC = {"setup.css": "text/css; charset=utf-8", "setup.js": "application/javascript; charset=utf-8"}
# Assistants (téléphone) : aucune ligne de script, formulaires vers soi seulement.
# Page finale (après « Oui, enregistrer » ou « Annuler ») : l'écoute LAN se ferme juste après la réponse, avant que le
# navigateur ne demande /static/setup.css. Ses styles (sous-ensemble mot pour mot de setup.css, vérifié par test) sont
# donc en ligne, autorisés par leur seule empreinte SHA-256 : ni 'unsafe-inline', ni script, ni écoute prolongée.
AGENTS_RESULT_CSS = (
    "*{box-sizing:border-box}"
    "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}"
    "body{margin:0;font-family:system-ui,-apple-system,\"Segoe UI\",\"Noto Sans\",sans-serif;background:#0d1117;"
    "color:#f0f3f6;line-height:1.4}"
    "main{max-width:520px;margin:0 auto;padding:20px 18px 40px}"
    "h1{font-size:28px;line-height:1.2;margin:6px 0 8px}"
    "p{font-size:17px;margin:10px 0}"
    ".lead{font-size:19px;color:#c9d1d9}"
    ".steps{display:flex;align-items:center;gap:10px;font-size:15px;color:#8b949e;margin:0 0 6px}"
    ".dots{display:inline-flex;gap:6px}"
    ".dot{width:28px;height:6px;border-radius:3px;background:#30363d}"
    ".dot.on{background:#2f81f7}"
    ".err{background:#6e1b1b;border:2px solid #f85149;padding:12px 16px;border-radius:12px;font-size:17px}"
    ".hint{color:#8b949e;font-size:15px}"
    ".status{list-style:none;margin:0;padding:0;display:grid;gap:6px}"
    ".st{display:flex;gap:10px;align-items:baseline;font-size:16px;color:#c9d1d9}"
    ".st b{flex:0 0 92px;font-size:15px;font-weight:700;padding:2px 8px;border-radius:8px;text-align:center}"
    ".st.yes b{background:#12361f;color:#7ee2a0}"
    ".st.no b{background:#2d333b;color:#adbac7}"
    ".recap{list-style:none;padding:0;margin:14px 0 0;display:grid;gap:10px}"
    ".recap li{padding:14px 16px;border-radius:14px;border:2px solid #30363d;background:#161b22;font-size:18px}"
    ".recap li > b{display:block;font-size:20px;margin-bottom:4px}"
    ".recap li > span{display:block}"
    ".recap small{display:block;color:#e3b341;font-size:15px;margin-top:6px}"
    ".recap.final .status{margin-top:6px}")
AGENTS_RESULT_CSS_HASH = "'sha256-%s'" % base64.b64encode(
    hashlib.sha256(AGENTS_RESULT_CSS.encode("utf-8")).digest()).decode("ascii")
AGENTS_CSP = ("default-src 'none'; style-src 'self' %s; img-src 'self'; form-action 'self'; base-uri 'none'; "
              "frame-ancestors 'none'" % AGENTS_RESULT_CSS_HASH)
AGENTS_STATIC = {"setup.css": "text/css; charset=utf-8"}
# Télécommande (téléphone) : un script de même origine (envoi sans rechargement), connexions vers soi seulement
REMOTE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
              "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
REMOTE_STATIC = {"remote.css": "text/css; charset=utf-8", "remote.js": "application/javascript; charset=utf-8"}
REMOTE_COOKIE = "otv_remote"
REMOTE_FORM_MAX = 512
REMOTE_RETRY_S = 30.0   # écoute impossible (adresse pas encore prête) : nouvel essai au plus toutes les 30 s
MAX_JSON = 2048
TV_KEY_ERROR = "tv_key"   # 403 de /api/tv/* : clé de page périmée (service redémarré) -> tv.js recharge /tv, borné
MAX_FORM = 4096
AGENT_LABELS = (("muse", "Muse"), ("hermit", "Hermit"), ("hermes", "Hermes"))
LEVEL_LABELS = (("none", "Aucun accès"), ("read", "Voir seulement"), ("feedback", "Voir + donner mon avis"),
                ("full", "Tout : avis + messages sur la TV"))


# Journal HTTP : erreurs et non-2xx toujours ; navigations de page (/tv, /setup) toujours (rares, bornées) ; succès
# des requêtes répétitives (poll /api/tv/*, /static/*, /healthz) non journalisés, un échantillon agrégé tous les N.
QUIET_OK_PREFIXES = ("/api/tv/", "/static/", "/r/cmd")
QUIET_OK_PATHS = ("/healthz",)
HTTP_OK_SAMPLE = 600
BUSY_LOG_INTERVAL_S = 60.0


class _Server(bounded.BoundedServerMixIn, http.server.HTTPServer):
    """HTTP à concurrence bornée (16 connexions servies, 503 immédiat au-delà) et échéance globale par connexion."""
    allow_reuse_address = True
    busy_response = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     b"Content-Length: 5\r\nRetry-After: 1\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                     b"busy\n")

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.quiet_ok = 0
        self.log_lock = threading.Lock()
        self.busy_logged_at = -1e9

    def on_busy(self):
        now = time.monotonic()
        if now - self.busy_logged_at >= BUSY_LOG_INTERVAL_S:
            self.busy_logged_at = now
            self.app.log("http_busy", port=self.server_address[1], refused=self.busy_count)


class _PerIPServer(_Server):
    """Serveur LAN permanent (télécommande) : en plus du plafond global, au plus max_per_ip connexions servies par
    adresse source ; au-delà, 503 immédiat. Un appareil du LAN ne peut pas occuper tous les emplacements."""
    max_per_ip = 4

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ip_lock = threading.Lock()
        self.ip_active = {}
        self.req_ip = {}

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self.ip_lock:
            if self.ip_active.get(ip, 0) >= self.max_per_ip:
                refuse = True
            else:
                refuse = False
                self.ip_active[ip] = self.ip_active.get(ip, 0) + 1
                self.req_ip[id(request)] = ip
        if refuse:
            self._slots_sem()
            self.busy_count += 1
            try:
                request.settimeout(bounded.BUSY_SEND_TIMEOUT_S)
                request.sendall(self.busy_response)
            except OSError:
                pass
            self.shutdown_request(request)
            self.on_busy()
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request):   # appelé une fois par connexion, sur tous les chemins
        with self.ip_lock:
            ip = self.req_ip.pop(id(request), None)
            if ip is not None:
                n = self.ip_active.get(ip, 1) - 1
                if n > 0:
                    self.ip_active[ip] = n
                else:
                    self.ip_active.pop(ip, None)
        super().shutdown_request(request)


class _Base(http.server.BaseHTTPRequestHandler):
    server_version = "OpenTV-Pi"
    sys_version = ""
    timeout = 10
    csp = TV_CSP
    referrer_policy = "no-referrer"

    @property
    def app(self):
        return self.server.app

    def setup(self):
        super().setup()
        bounded.install_deadline_reader(self)   # échéance globale : un client lent est coupé (slowloris)

    def log_request(self, code="-", size="-"):   # chemin sans requête : le jeton ne fuit jamais
        path = urllib.parse.urlsplit(self.path).path[:64]
        status = int(code) if str(code).isdigit() else 0
        if 200 <= status < 400 and (path.startswith(QUIET_OK_PREFIXES) or path in QUIET_OK_PATHS):
            with self.server.log_lock:
                self.server.quiet_ok += 1
                sample = self.server.quiet_ok % HTTP_OK_SAMPLE == 0
            if sample:
                self.app.log("http_ok_sample", count=HTTP_OK_SAMPLE, port=self.server.server_address[1])
            return
        self.app.log("http", method=self.command[:8] if self.command else "-", path=path, status=status)

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:   # écriture bornée elle aussi par l'échéance de la connexion (au moins 0,5 s)
            self.connection.settimeout(max(0.5, self.deadline - time.monotonic()))
        except (AttributeError, OSError):
            pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", self.csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", self.referrer_policy)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False, sort_keys=True), "application/json; charset=utf-8")

    def read_body(self, cap):
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if n < 0 or n > cap:
            return None
        return self.rfile.read(n)

    def static_path(self, name):
        if name == "qrcode.js":
            return self.app.cfg.qrcode_js
        return os.path.join(self.app.cfg.share_dir, name)

    def send_static(self, name, types):
        if name not in types:
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        try:
            with open(self.static_path(name), "rb") as f:
                data = f.read(2 * 1024 * 1024)
        except OSError:
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        self.send(200, data, types[name], {"Cache-Control": "no-cache"})


class TVHandler(_Base):
    csp = TV_CSP

    def _guard(self):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self.send(403, "interdit", "text/plain; charset=utf-8")
                return False
        except ValueError:
            self.send(403, "interdit", "text/plain; charset=utf-8")
            return False
        port = self.server.server_address[1]
        if self.headers.get("Host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            self.send(421, "hôte refusé", "text/plain; charset=utf-8")
            return False
        allowed = self.app.tv_client_uids
        if allowed is not None and urllib.parse.urlsplit(self.path).path != "/healthz":
            if self.app.peer_uid_fn(self.client_address[0], self.client_address[1], port) not in allowed:
                self.send(403, "réservé au kiosque", "text/plain; charset=utf-8")
                return False
        return True

    def _key_ok(self):
        k = self.headers.get("X-OpenTV-TV") or ""
        return hmac.compare_digest(k.encode("utf-8"), self.app.tv_key.encode("utf-8"))

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "version": VERSION})
        if path in ("/", "/tv"):
            try:
                with open(os.path.join(self.app.cfg.share_dir, "tv.html"), encoding="utf-8") as f:
                    page = f.read()
            except OSError:
                return self.send(500, "page TV absente", "text/plain; charset=utf-8")
            return self.send(200, page.replace("{{TV_KEY}}", self.app.tv_key).replace("{{VERSION}}", VERSION))
        if path.startswith("/static/"):
            return self.send_static(path[len("/static/"):], TV_STATIC)
        if path.startswith("/api/tv/"):
            if not self._key_ok():   # code explicite : la page TV sait qu'elle doit se recharger (clé tournée)
                return self.send_json(403, {"ok": False, "error": TV_KEY_ERROR})
            if path == "/api/tv/state":
                return self.send_json(200, self.app.tv_state())
            if path == "/api/tv/foryou":
                return self.send_json(200, self.app.tv_foryou())
            if path == "/api/tv/remote":   # longue requête bornée : touches du téléphone pour la page TV
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, max_num_fields=1)
                a = (q.get("after") or ["-1"])[0]
                after = int(a) if re.fullmatch(r"-1|[0-9]{1,12}", a) else -1
                return self.send_json(200, self.app.remote_events(after))
        self.send(404, "introuvable", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/tv/"):
            return self.send_json(403, {"ok": False, "error": "forbidden"})
        if not self._key_ok():
            return self.send_json(403, {"ok": False, "error": TV_KEY_ERROR})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self.send_json(415, {"ok": False, "error": "content_type"})
        raw = self.read_body(MAX_JSON)
        if raw is None:
            return self.send_json(413, {"ok": False, "error": "too_large"})
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self.send_json(400, {"ok": False, "error": "bad_json"})
        if not isinstance(body, dict):
            return self.send_json(400, {"ok": False, "error": "bad_json"})
        if path == "/api/tv/feedback":
            state, msg = self.app.tv_feedback(body.get("id"), body.get("action"))
            return self.send_json(200 if state is not None else 400, {"ok": state is not None, "state": state,
                                                                      "message": msg})
        if path == "/api/tv/setup":
            self.app.request_setup()
            return self.send_json(200, {"ok": True})
        if path == "/api/tv/remote/revoke":
            self.app.remote_revoke()
            return self.send_json(200, {"ok": True})
        if path == "/api/tv/agents":   # Réglages « Gérer les assistants » (confirmé sur la TV) : QR dédié
            ok, why = self.app.request_agents()
            seq = self.app.agents_seq()   # résultats antérieurs à cette ouverture : jamais annoncés par la TV
            ws = getattr(self.server, "webservers", None)
            if ok and ws is not None:
                ws.sync_lan()   # page ouverte tout de suite : le QR affiché fonctionne au premier scan
            # issue déjà connue de CETTE ouverture (écoute impossible ⇒ « unavailable ») : la TV l'annonce une fois et
            # n'ouvre jamais la feuille ; « seq » reste la référence (résultats antérieurs jamais annoncés)
            last = self.app.agents_last_result()
            return self.send_json(200 if ok else 409, {"ok": ok, "error": why, "seq": seq, "last": last})
        if path == "/api/tv/agents/cancel":
            self.app.cancel_agents("tv")
            ws = getattr(self.server, "webservers", None)
            if ws is not None:
                ws.sync_lan()
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"ok": False, "error": "not_found"})


# --------------------------------------------------------------------------- page de configuration (téléphone)
# Assistant en 3 étapes, rendu par le serveur (fonctionne sans JavaScript) :
#   1. « Quel Wi-Fi ? »      GET /setup?t=…               grandes cartes : réseaux proches, garder la connexion, autre
#   2. « Mot de passe »      GET /setup?t=…&net=…&ssid=…  un seul champ + « Connecter la TV » (POST final)
#   3. « C'est prêt »        réponse du POST
# Les GET ne consomment jamais le jeton (au plus 4 champs : t, net, ssid) ; le mot de passe ne voyage que dans le POST
# final, dont les champs restent name/net/ssid/psk/hidden/agent_* (contrat de App.setup_submit). Rien de secret
# n'est réaffiché : le mot de passe n'est jamais réécrit dans la page, le jeton seulement dans le formulaire/lien.
STEPS = 3


def _page(title, inner, script=False):
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><meta name=\"color-scheme\" content=\"dark\">"
            "<title>%s</title><link rel=\"stylesheet\" href=\"/static/setup.css\">%s</head>"
            "<body><main>%s</main></body></html>"
            % (html.escape(title), "<script src=\"/static/setup.js\" defer></script>" if script else "", inner))


def _progress(n):
    dots = "".join("<span class=\"dot%s\"></span>" % (" on" if i <= n else "") for i in range(1, STEPS + 1))
    return ("<p class=\"steps\"><span class=\"dots\" aria-hidden=\"true\">%s</span>Étape %d sur %d</p>"
            % (dots, n, STEPS))


def _url(token, **q):
    pairs = [("t", token)] + [(k, v) for k, v in q.items() if v is not None]
    return html.escape("/setup?" + urllib.parse.urlencode(pairs), quote=True)


_level_of = appmod.level_of


def _online(ctx):
    return ctx.get("net_mode") in ("ethernet", "wifi")


def _signal_html(sig):
    n = wifiscan.bars(sig)
    word = {4: "excellent", 3: "bon", 2: "moyen", 1: "faible"}[n]
    return ("<span class=\"sig s%d\" role=\"img\" aria-label=\"Signal %s\"><i></i><i></i><i></i><i></i></span>"
            % (n, word))


def wizard_plan(ctx, error=None, values=None, choice=None):
    """Étape à afficher -> {"step": 1|2, "mode": None|"keep"|"wifi"|"other", "ssid": str|None, "error": str|None}.

    choice = (net, ssid) d'un GET de navigation ; values = champs renvoyés après un POST refusé."""
    def valid_ssid(x):
        try:
            return validate.ssid(x)
        except ValueError:
            return None
    if values is not None:   # POST refusé : réafficher l'étape fautive, jamais le mot de passe
        net = values.get("net")
        if error in (appmod.ERR_FORM_NET, appmod.ERR_FORM_OFFLINE) or net not in ("keep", "wifi"):
            return {"step": 1, "mode": None, "ssid": None, "error": error}
        if net == "keep":
            return {"step": 2, "mode": "keep", "ssid": None, "error": error}
        ssid = valid_ssid(values.get("ssid"))
        manual = ssid is None or error == appmod.ERR_FORM_SSID or values.get("hidden") == "1"
        return {"step": 2, "mode": "other" if manual else "wifi", "ssid": ssid, "error": error}
    if choice is None:
        return {"step": 1, "mode": None, "ssid": None, "error": error}
    net, ssid = choice
    if net == "keep":
        if not _online(ctx):
            return {"step": 1, "mode": None, "ssid": None, "error": appmod.ERR_FORM_OFFLINE}
        return {"step": 2, "mode": "keep", "ssid": None, "error": None}
    if net == "wifi":
        ok = valid_ssid(ssid)
        if ok is None:
            return {"step": 1, "mode": None, "ssid": None, "error": appmod.ERR_FORM_SSID}
        return {"step": 2, "mode": "wifi", "ssid": ok, "error": None}
    if net == "other":
        return {"step": 2, "mode": "other", "ssid": valid_ssid(ssid) if ssid else None, "error": None}
    return {"step": 1, "mode": None, "ssid": None, "error": None}


def _step_network(token, ctx, plan, networks):
    e = html.escape
    parts = [_progress(1), "<h1>Quel est ton Wi-Fi\u00a0?</h1><p class=\"lead\">%s</p>"
             % ("Touche le nom de ton Wi-Fi." if networks else "Écris le nom de ton Wi-Fi.")]
    if plan["error"]:
        parts.append("<p class=\"err\" role=\"alert\">%s</p>" % e(plan["error"]))
    if networks:
        parts.append("<form method=\"get\" action=\"/setup\" class=\"choices\">"
                     "<input type=\"hidden\" name=\"t\" value=\"%s\">"
                     "<input type=\"hidden\" name=\"net\" value=\"wifi\">"
                     % e(token, quote=True))
        for n in networks:
            parts.append("<button type=\"submit\" class=\"choice\" name=\"ssid\" value=\"%s\">"
                         "<span class=\"name\">%s</span>%s</button>"
                         % (e(n["ssid"], quote=True), e(n["ssid"]), _signal_html(n["signal"])))
        parts.append("</form>")
    else:
        parts.append("<p class=\"empty\">Aucun Wi-Fi trouvé pour l'instant.</p>")
    parts.append("<div class=\"choices\">")
    parts.append("<a class=\"choice%s\" href=\"%s\"><span class=\"name\">%s</span></a>"
                 % (" other" if networks else "", _url(token, net="other"),
                    "Mon Wi-Fi n'est pas dans la liste" if networks else "Écrire le nom de mon Wi-Fi"))
    if _online(ctx):
        what = "le câble Ethernet" if ctx["net_mode"] == "ethernet" else "le Wi-Fi actuel"
        parts.append("<a class=\"choice keep\" href=\"%s\"><span class=\"name\">Garder %s</span>"
                     "<span class=\"sub\">Rien à changer</span></a>" % (_url(token, net="keep"), e(what)))
    parts.append("</div><p class=\"more\"><a href=\"%s\">Chercher encore</a></p>" % _url(token))
    return "".join(parts)


def _agent_fields(ctx, values, plan):
    """Muse : une case simple (« apprendre de mes goûts » = feedback), décochée par défaut. Hermit/Hermes : options
    avancées, « Aucun accès » par défaut. Une reconfiguration reprend la politique enregistrée (jamais d'élévation
    silencieuse, jamais de révocation surprise)."""
    e, v = html.escape, values or {}
    pol = ctx.get("policy") or {}
    cur = {}
    for aid, _ in AGENT_LABELS:
        x = v.get("agent_" + aid)
        cur[aid] = x if x in dict(LEVEL_LABELS) else _level_of(pol.get(aid))
    if values is not None and v.get("agent_muse") is None:
        cur["muse"] = "none"   # case décochée au POST précédent
    muse_simple = cur["muse"] in ("none", "feedback")
    simple, adv = [], []
    if muse_simple:
        simple.append("<label class=\"check\"><input type=\"checkbox\" name=\"agent_muse\" value=\"feedback\"%s>"
                      "<span>Laisser Muse apprendre de mes goûts<small>Seulement «\u00a0j'aime\u00a0» et "
                      "«\u00a0pas pour moi\u00a0». Facultatif.</small></span></label>"
                      % (" checked" if cur["muse"] == "feedback" else ""))
    for aid, label in AGENT_LABELS:
        if aid == "muse" and muse_simple:
            continue
        opts = "".join("<option value=\"%s\"%s>%s</option>" % (k, " selected" if k == cur[aid] else "", e(t))
                       for k, t in LEVEL_LABELS)
        adv.append("<label>%s<select name=\"agent_%s\">%s</select></label>" % (e(label), aid, opts))
    is_open = plan["error"] in (appmod.ERR_FORM_NAME, appmod.ERR_FORM_AGENT) or not muse_simple or \
        any(cur[a] != "none" for a in ("hermit", "hermes"))
    name = v.get("name") if values is not None else (ctx.get("name") or "Salon")
    return "".join(simple), (
        "<details class=\"adv\"%s><summary>Plus d'options</summary>"
        "<label>Nom de la TV<input name=\"name\" maxlength=\"32\" required value=\"%s\" autocomplete=\"off\"></label>"
        "%s<p class=\"hint\">Les assistants Hermit et Hermes n'ont aucun accès par défaut. Aucun assistant ne reçoit "
        "ton mot de passe Wi-Fi.</p></details>"
        % (" open" if is_open else "", e(name or "", quote=True), "".join(adv)))


def _step_finish(token, ctx, plan, values, ios=False):
    e, mode = html.escape, plan["mode"]
    parts = [_progress(2)]
    if mode == "keep":
        parts.append("<h1>Presque fini</h1><p class=\"lead\">La TV garde sa connexion actuelle.</p>")
    elif mode == "wifi":
        parts.append("<h1>Mot de passe du Wi-Fi</h1><p class=\"lead\">Wi-Fi : <strong class=\"ssid\">%s</strong></p>"
                     % e(plan["ssid"]))
    else:
        parts.append("<h1>Ton Wi-Fi</h1><p class=\"lead\">Écris le nom et le mot de passe de ton Wi-Fi.</p>")
    if plan["error"]:
        parts.append("<p class=\"err\" role=\"alert\">%s</p>" % e(plan["error"]))
    parts.append("<form method=\"post\" action=\"/setup\" id=\"finish\" autocomplete=\"off\">"
                 "<input type=\"hidden\" name=\"t\" value=\"%s\">" % e(token, quote=True))
    if mode == "keep":
        parts.append("<input type=\"hidden\" name=\"net\" value=\"keep\">")
    else:
        parts.append("<input type=\"hidden\" name=\"net\" value=\"wifi\">")
        if mode == "wifi":
            parts.append("<input type=\"hidden\" name=\"ssid\" value=\"%s\">" % e(plan["ssid"], quote=True))
        else:
            parts.append("<label>Nom du Wi-Fi<input name=\"ssid\" id=\"ssid\" maxlength=\"32\" required "
                         "autocapitalize=\"none\" autocorrect=\"off\" spellcheck=\"false\" "
                         "value=\"%s\"%s></label>"
                         % (e(plan["ssid"] or "", quote=True), "" if plan["ssid"] else " autofocus"))
        # jamais de value= : le mot de passe n'est pas réécrit dans la page, même après une erreur
        ptype = "text" if ios else "password"
        pclass = " class=\"ios-text-mask\"" if ios else ""
        parts.append("<label>Mot de passe<span class=\"pw\"><input name=\"psk\" id=\"psk\" type=\"%s\"%s "
                     "minlength=\"8\" maxlength=\"64\" required autocomplete=\"off\" inputmode=\"text\" "
                     "autocapitalize=\"none\" autocorrect=\"off\" spellcheck=\"false\" enterkeyhint=\"go\" "
                     "data-1p-ignore=\"true\" data-lpignore=\"true\"%s></span></label>"
                     % (ptype, pclass, " autofocus" if mode == "wifi" or (mode == "other" and plan["ssid"]) else ""))
        if mode == "other":
            hidden = (values or {}).get("hidden") == "1"
            parts.append("<label class=\"check\"><input type=\"checkbox\" name=\"hidden\" value=\"1\"%s>"
                         "<span>Wi-Fi masqué<small>Seulement s'il n'apparaît jamais dans la liste.</small></span>"
                         "</label>" % (" checked" if hidden else ""))
    simple, adv = _agent_fields(ctx, values, plan)
    parts.append(simple)
    parts.append("<button type=\"submit\" class=\"primary\">%s</button>"
                 % ("Terminer" if mode == "keep" else "Connecter la TV"))
    parts.append(adv)
    parts.append("</form><p class=\"more\"><a href=\"%s\">← Choisir un autre Wi-Fi</a></p>" % _url(token))
    if mode != "keep":
        parts.append("<p class=\"hint\">Le mot de passe va seulement à la boîte OpenTV. Il n'est jamais affiché "
                     "ni enregistré dans un journal.</p>")
    return "".join(parts)


def setup_form(token, ctx, error=None, values=None, networks=None, choice=None, plan=None, ios=False):
    """Page de l'assistant (étape 1 ou 2). networks : réseaux proches validés (wifiscan), utilisés à l'étape 1."""
    plan = plan or wizard_plan(ctx, error, values, choice)
    if plan["step"] == 1:
        inner = _step_network(token, ctx, plan, networks or [])
    else:
        inner = _step_finish(token, ctx, plan, values, ios=ios)
    return _page("Configurer OpenTV", inner, script=plan["step"] == 2)


REMOTE_LATER = ("<p class=\"hint\">Télécommande sur téléphone (facultatif) : quand OpenTV s'affiche, garde ce "
                "téléphone sur le <strong>même Wi-Fi que la TV</strong> et scanne le QR «\u00a0Télécommande\u00a0» "
                "à droite de l'écran.</p>")


def _agents_note(agents_saved):
    """Succès partiel (Wi-Fi/nom appliqués, assistants non enregistrés) : phrase simple, sans détail technique."""
    if agents_saved is not False:
        return ""
    return "<p class=\"err\" role=\"status\">%s</p>" % html.escape(appmod.MSG_AGENTS_NOT_SAVED)


def setup_result(kind, remote_link=None, via_hotspot=False, agents_saved=True):
    note = _agents_note(agents_saved) if kind in ("done", "applying") else ""
    if kind == "done" and remote_link:   # télécommande proposée dans le parcours : un toucher, aucun code à copier
        return _page("OpenTV", _progress(3) + "<h1>C'est prêt !</h1>"
                     "<p class=\"lead\">OpenTV s'affiche maintenant sur la TV.</p>" + note +
                     "<a class=\"primary\" href=\"%s\">Utiliser ce téléphone comme télécommande</a>"
                     "<p class=\"hint\">Facultatif. Ce téléphone doit rester sur le même Wi-Fi que la TV. Tu peux "
                     "aussi fermer cette page.</p>" % html.escape(remote_link, quote=True))
    if kind == "applying":
        if via_hotspot:   # le téléphone est sur le Wi-Fi temporaire, qui va disparaître : reprise expliquée
            after = ("<p class=\"hint\">Ce téléphone va perdre le Wi-Fi temporaire «\u00a0OpenTV\u00a0» : c'est "
                     "normal. Remets-le ensuite sur ton Wi-Fi habituel (le même que la TV) : la télécommande sur "
                     "téléphone se trouve alors par le QR «\u00a0Télécommande\u00a0» à droite de l'écran.</p>")
        else:
            after = REMOTE_LATER
        return _page("OpenTV", _progress(3) + "<h1>C'est presque fini !</h1>"
                     "<p class=\"lead\">Regarde la TV : elle se connecte à ton Wi-Fi (moins d'une minute).</p>"
                     "<p>Si le mot de passe est refusé, la TV affiche un nouveau QR : scanne-le pour réessayer.</p>"
                     + note + after)
    if kind == "done":
        return _page("OpenTV", _progress(3) + "<h1>C'est prêt !</h1>"
                     "<p class=\"lead\">OpenTV s'affiche maintenant sur la TV.</p>" + note +
                     "<p>Tu peux fermer cette page.</p>" + REMOTE_LATER)
    if kind == "bad_request":
        return _page("OpenTV", "<h1>Adresse invalide</h1><p class=\"lead\">Scanne de nouveau le QR affiché sur la TV."
                               "</p>")
    return _page("OpenTV", "<h1>Code expiré ou déjà utilisé</h1><p class=\"lead\">Scanne le nouveau QR affiché sur "
                           "la TV.</p>")


class LANHandler(_Base):
    csp = SETUP_CSP

    def _ios(self):
        ua = self.headers.get("User-Agent") or ""
        return any(x in ua for x in ("iPhone", "iPad", "iPod"))

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith("/static/"):
            return self.send_static(u.path[len("/static/"):], SETUP_STATIC)
        if u.path != "/setup":
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        try:   # requête trop large ou malformée : 400 borné, sans trace ; le jeton n'est jamais consommé par un GET
            q = urllib.parse.parse_qs(u.query, max_num_fields=4)
        except ValueError:
            return self.send(400, setup_result("bad_request"))
        one = lambda k: q[k][0] if len(q.get(k) or ()) == 1 else None   # noqa: E731
        tok = one("t") or ""
        if not self.app.check_token(tok):
            return self.send(403, setup_result("denied"))
        ctx = self.app.setup_context()
        choice = (one("net"), one("ssid")) if "net" in q else None
        plan = wizard_plan(ctx, choice=choice)
        networks = None
        if plan["step"] == 1:   # balayage borné et mis en cache, seulement avec un jeton valide
            networks = self.app.setup_networks(tok, client=self.client_address[0])
            if networks is None:
                return self.send(403, setup_result("denied"))
        self.send(200, setup_form(tok, ctx, networks=networks, plan=plan, ios=self._ios()))

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/setup":
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return self.send(415, setup_result("denied"))
        raw = self.read_body(MAX_FORM)
        if raw is None:
            return self.send(413, setup_result("denied"))
        try:
            q = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=False,
                                      max_num_fields=20)
        except (ValueError, UnicodeDecodeError):
            return self.send(400, setup_result("denied"))
        form = {k: v[0] for k, v in q.items() if len(v) == 1}
        via_hotspot = self.app.setup_context()["net_mode"] == "hotspot"
        outcome = {}
        code, kind, err = self.app.setup_submit(form, client=self.client_address[0], outcome=outcome)
        saved = outcome.get("agents_saved", True)
        if kind == "done":   # ouvrir tout de suite la télécommande pour que le bouton de la page fonctionne
            ws = getattr(self.server, "webservers", None)
            if ws is not None:
                ws.sync_remote()
            return self.send(code, setup_result(kind, self.app.remote_pair_link(), agents_saved=saved))
        if kind == "applying":
            return self.send(code, setup_result(kind, via_hotspot=via_hotspot, agents_saved=saved))
        if kind == "form":   # jamais le mot de passe : il n'est pas dans vals
            vals = {k: form.get(k) for k in ("name", "net", "ssid", "hidden", "agent_muse", "agent_hermit",
                                             "agent_hermes")}
            ctx = self.app.setup_context()
            plan = wizard_plan(ctx, err, vals)
            networks = None
            if plan["step"] == 1:
                networks = self.app.setup_networks(form.get("t", ""), client=self.client_address[0]) or []
            return self.send(code, setup_form(form.get("t", ""), ctx, err, vals, networks=networks, plan=plan,
                                              ios=self._ios()))
        self.send(code, setup_result(kind))


# --------------------------------------------------------------------------- gestion des assistants (téléphone)
# Parcours dédié, en 3 étapes courtes, rendu par le serveur et sans aucun script :
#   1. « Assistants de la TV »  GET /assistants?t=…                 état honnête + un choix par assistant
#   2. « Confirmer »            GET /assistants?t=…&agent_muse=…    récapitulatif, « Oui, enregistrer » (POST final)
#   3. « C'est enregistré »     réponse du POST                     la TV revient à l'accueil et le confirme
# Les GET ne consomment jamais le jeton (au plus 4 champs : t + un par assistant). Le POST porte t, c (CSRF), action
# (save|cancel) et agent_* ; App.agents_submit reste seul juge. Aucune page ne contient de jeton d'agent, de chemin
# de fichier client, de commande ou d'adresse technique.
AGENT_STEPS = 3
LEVEL_SHORT = {"none": "Aucun accès", "read": "Voir seulement", "feedback": "Voir + donner mon avis",
               "full": "Tout : avis + messages sur la TV"}
AGENTS_DENIED = ("Ce QR a expiré ou a déjà servi. Sur la TV : Réglages, puis «\u00a0Gérer les assistants\u00a0» pour "
                 "en afficher un nouveau.")


def _apage(title, inner, final=False):
    """final=True : page servie juste avant la fermeture de l'écoute ⇒ styles en ligne (empreinte dans la CSP),
    aucune requête de feuille de style qui arriverait sur un port déjà fermé."""
    css = ("<style>%s</style>" % AGENTS_RESULT_CSS) if final else "<link rel=\"stylesheet\" href=\"/static/setup.css\">"
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"same-origin\"><meta name=\"color-scheme\" content=\"dark\">"
            "<title>%s</title>%s</head>"
            "<body><main class=\"agents\">%s</main></body></html>" % (html.escape(title), css, inner))


def _aprogress(n):
    dots = "".join("<span class=\"dot%s\"></span>" % (" on" if i <= n else "") for i in range(1, AGENT_STEPS + 1))
    return ("<p class=\"steps\"><span class=\"dots\" aria-hidden=\"true\">%s</span>Étape %d sur %d</p>"
            % (dots, n, AGENT_STEPS))


def _ago(sec):
    if sec < 90:
        return "à l'instant"
    if sec < 5400:
        return "il y a %d min" % max(2, round(sec / 60.0))
    return "il y a %d h" % round(sec / 3600.0)


def _agent_status_html(x):
    """Deux lignes distinctes, jamais confondues : « Autorisé » (ce que tu permets) et « Connecté » (installé sur la
    boîte, avec sa clé). Une autorisation seule n'est jamais présentée comme une connexion."""
    e = html.escape
    auth = ("<li class=\"st %s\"><b>Autorisé</b><span>%s</span></li>"
            % ("yes" if x["authorized"] else "no", e(LEVEL_SHORT[x["level"]] if x["authorized"] else "Non")))
    if x["connected"]:
        act = (" · actif " + _ago(x["seen_s"])) if x.get("seen_s") is not None else " · pas encore actif"
        conn = ("<li class=\"st yes\"><b>Connecté</b><span>Oui, installé sur la boîte (%s)%s</span></li>"
                % (e(LEVEL_SHORT[x["connected_level"]].lower()), e(act)))
    else:
        conn = "<li class=\"st no\"><b>Connecté</b><span>Non, pas installé sur la boîte</span></li>"
    return "<ul class=\"status\">%s%s</ul>" % (auth, conn)


def _cancel_form(token, csrf, label="Annuler, rien ne change"):
    e = html.escape
    return ("<form method=\"post\" action=\"/assistants\" class=\"cancel\">"
            "<input type=\"hidden\" name=\"t\" value=\"%s\"><input type=\"hidden\" name=\"c\" value=\"%s\">"
            "<input type=\"hidden\" name=\"action\" value=\"cancel\">"
            "<button type=\"submit\" class=\"secondary\">%s</button></form>"
            % (e(token, quote=True), e(csrf, quote=True), e(label)))


def agents_choose_page(token, csrf, lst, error=None, values=None):
    """Étape 1 : un bloc par assistant (état réel + liste de choix pré-remplie avec la politique enregistrée)."""
    e, v = html.escape, values or {}
    parts = [_aprogress(1), "<h1>Assistants de la TV</h1>"
             "<p class=\"lead\">Choisis ce que chaque assistant peut faire. Le Wi-Fi et le nom de la TV ne changent "
             "pas.</p>"]
    if error:
        parts.append("<p class=\"err\" role=\"alert\">%s</p>" % e(error))
    parts.append("<form method=\"get\" action=\"/assistants\" id=\"choose\">"
                 "<input type=\"hidden\" name=\"t\" value=\"%s\">" % e(token, quote=True))
    for x in lst:
        cur = v.get("agent_" + x["id"])
        labels = list(LEVEL_LABELS)
        note = ""
        if x["over"]:   # installé avec plus de droits que son autorisation : par défaut, rien ne change pour lui
            labels.insert(0, (appmod.AGENT_KEEP, "Ne rien changer pour lui"))   # court : lisible sur 390 px
            note = ("<p class=\"hint\">Installé avec plus de droits que son autorisation. «\u00a0Ne rien changer\u00a0» "
                    "le laisse tel quel ; un autre choix l'y ramène tout de suite.</p>")
        if cur not in dict(labels):
            cur = appmod.AGENT_KEEP if x["over"] else x["level"]
        opts = "".join("<option value=\"%s\"%s>%s</option>" % (k, " selected" if k == cur else "", e(t))
                       for k, t in labels)
        parts.append("<section class=\"agent\"><h2>%s</h2>%s%s<label>Ce que %s peut faire"
                     "<select name=\"agent_%s\">%s</select></label></section>"
                     % (e(x["name"]), _agent_status_html(x), note, e(x["name"]), x["id"], opts))
    parts.append("<button type=\"submit\" class=\"primary\">Continuer</button></form>")
    parts.append(_cancel_form(token, csrf))
    parts.append("<p class=\"hint\">«\u00a0Autorisé\u00a0» : ce que tu permets. «\u00a0Connecté\u00a0» : l'assistant "
                 "est vraiment installé sur la boîte. Autoriser un assistant ne l'installe pas.</p>")
    return _apage("Assistants OpenTV", "".join(parts))


def agents_plan(lst, values):
    """Choix complets et valides -> [{x, new, change}] ; sinon None (retour à l'étape 1). « change » suit
    exactement la règle d'écriture (appmod.choice_changes) : une ligne « Inchangé » n'est jamais écrite."""
    out = []
    for x in lst:
        new = values.get("agent_" + x["id"])
        if new not in LEVEL_SHORT and new != appmod.AGENT_KEEP:
            return None
        out.append({"x": x, "new": new, "change": appmod.choice_changes(x, new)})
    return out


def _consequence(x, new):
    """Effet réel d'un choix sur un assistant (jamais d'affirmation de connexion pour un assistant absent)."""
    rank = {"none": 0, "read": 1, "feedback": 2, "full": 3}
    if x["connected"] and new == "none":
        return "Il sera déconnecté tout de suite (il faudra le réinstaller pour lui redonner accès)."
    if x["connected"] and rank[new] < rank[x["connected_level"]]:
        return "Il perd tout de suite les droits retirés."
    if not x["connected"] and new != "none":
        return "Pas installé sur la boîte : cette autorisation ne le connecte pas."
    if x["connected"] and rank[new] > rank[x["connected_level"]]:
        return "Ses droits actuels restent les mêmes tant qu'il n'est pas réinstallé sur la boîte."
    return None


def agents_confirm_page(token, csrf, plan, fp):
    e = html.escape
    changes = [p for p in plan if p["change"]]
    parts = [_aprogress(2), "<h1>Confirmer</h1>"]
    if not changes:
        parts.append("<p class=\"lead\">Aucun changement.</p>")
    else:
        parts.append("<p class=\"lead\">Vérifie, puis enregistre.</p>")
    parts.append("<ul class=\"recap\">")
    for p in plan:
        x = p["x"]
        if p["change"] and p["new"] == x["level"]:   # même niveau, mais l'identité installée y est ramenée
            line = "Connecté (%s) → <strong>%s</strong>" % (e(LEVEL_SHORT[x["connected_level"]].lower()),
                                                            e(LEVEL_SHORT[p["new"]]))
        elif p["change"]:
            line = "%s → <strong>%s</strong>" % (e(LEVEL_SHORT[x["level"]]), e(LEVEL_SHORT[p["new"]]))
        else:
            line = "Inchangé : %s" % e(LEVEL_SHORT[x["level"]])
        if p["change"]:
            cons = _consequence(x, p["new"])
        elif x["connected"]:   # vrai pour une identité au-delà de son autorisation : elle n'est pas touchée
            cons = "Reste connecté (%s) : rien ne change pour lui." % LEVEL_SHORT[x["connected_level"]].lower()
        else:
            cons = None
        parts.append("<li><b>%s</b><span>%s</span>%s</li>" % (e(x["name"]), line,
                                                              "<small>%s</small>" % e(cons) if cons else ""))
    parts.append("</ul>")
    if changes:
        parts.append("<form method=\"post\" action=\"/assistants\" id=\"save\">"
                     "<input type=\"hidden\" name=\"t\" value=\"%s\"><input type=\"hidden\" name=\"c\" value=\"%s\">"
                     "<input type=\"hidden\" name=\"action\" value=\"save\">"
                     "<input type=\"hidden\" name=\"v\" value=\"%s\">" % (e(token, quote=True), e(csrf, quote=True),
                                                                        e(fp, quote=True)))
        for p in plan:   # ligne inchangée ⇒ « keep » : le serveur n'écrit que les lignes réellement modifiées
            parts.append("<input type=\"hidden\" name=\"agent_%s\" value=\"%s\">"
                         % (p["x"]["id"], p["new"] if p["change"] else appmod.AGENT_KEEP))
        parts.append("<button type=\"submit\" class=\"primary\">Oui, enregistrer</button></form>")
        parts.append(_cancel_form(token, csrf))
    else:
        parts.append(_cancel_form(token, csrf, "Terminer sans rien changer"))
    parts.append("<p class=\"more\"><a href=\"%s\">← Modifier</a></p>"
                 % html.escape("/assistants?" + urllib.parse.urlencode([("t", token)]), quote=True))
    return _apage("Assistants OpenTV", "".join(parts))


def agents_result_page(kind, lst=None, error=None, changed=None):
    """Pages finales : styles en ligne (l'écoute se ferme juste après la réponse)."""
    e = html.escape
    page = lambda inner: _apage("Assistants OpenTV", inner, final=True)   # noqa: E731
    if kind == "saved":
        ch = changed or {}
        rows = "".join("<li><b>%s</b><span>%s</span>%s</li>"
                       % (e(x["name"]), "Modifié" if x["id"] in ch else "Inchangé", _agent_status_html(x))
                       for x in (lst or []))
        return page(_aprogress(3) + "<h1>C'est enregistré</h1>"
                    "<p class=\"lead\">Regarde la TV : elle revient à l'accueil et confirme le changement.</p>"
                    "<ul class=\"recap final\">%s</ul><p>Tu peux fermer cette page.</p>" % rows)
    if kind == "cancelled":
        return page("<h1>Rien n'a changé</h1><p class=\"lead\">Les assistants gardent leurs réglages. Tu peux fermer "
                    "cette page.</p>")
    if kind == "unchanged":
        return page("<h1>Rien n'a changé</h1><p class=\"lead\">Aucun choix ne modifiait un assistant : rien n'a été "
                    "écrit. Tu peux fermer cette page.</p>")
    if kind == "limited":
        return page("<h1>Trop d'essais</h1><p class=\"lead\">Attends une minute, puis scanne le QR affiché sur la "
                    "TV.</p>")
    if kind == "refused":
        return page("<h1>Refusé</h1><p class=\"lead\">Requête venue d'une autre page. Scanne le QR affiché sur la "
                    "TV.</p>")
    if kind == "bad_request":
        return page("<h1>Adresse invalide</h1><p class=\"lead\">Scanne de nouveau le QR affiché sur la TV.</p>")
    if kind == "error":
        return page("<h1>Rien n'a changé</h1><p class=\"err\" role=\"alert\">%s</p>" % e(error or ""))
    return page("<h1>Code expiré ou déjà utilisé</h1><p class=\"lead\">%s</p>" % e(AGENTS_DENIED))


def same_origin(headers, origin):
    """Origin exacte (« null » toujours refusé) ; sans Origin : Sec-Fetch-Site et Referer cohérents."""
    o = headers.get("Origin")
    if o is not None:
        return o.strip().lower() == origin
    sfs = headers.get("Sec-Fetch-Site")
    if sfs is not None and sfs not in ("same-origin", "none"):
        return False
    ref = headers.get("Referer")
    return not ref or ref.lower().startswith(origin + "/")


class AgentsHandler(_Base):
    """Téléphone -> /assistants (gestion des assistants seulement). Pair IPv4 privé, Host = adresse LAN liée avec le
    port réel, POST : Origin exacte + jeton CSRF + jeton QR à usage unique. Aucun /setup, aucun balayage Wi-Fi,
    aucune action réseau : ce serveur n'existe que pendant le mode « assistants »."""
    csp = AGENTS_CSP
    referrer_policy = "same-origin"

    def _peer_host_ok(self):
        srv = self.server
        if not srv.peer_ok(self.client_address[0]):
            self.send(403, "interdit", "text/plain; charset=utf-8")
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        if host != "%s:%d" % (srv.lan_ip, srv.server_address[1]):
            self.send(421, "hôte refusé", "text/plain; charset=utf-8")
            return False
        self.origin = "http://" + host
        return True

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if not self._peer_host_ok():
            return
        if u.path.startswith("/static/"):
            return self.send_static(u.path[len("/static/"):], AGENTS_STATIC)
        if u.path != "/assistants":
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        try:
            q = urllib.parse.parse_qs(u.query, max_num_fields=4)
        except ValueError:
            return self.send(400, agents_result_page("bad_request"))
        one = lambda k: q[k][0] if len(q.get(k) or ()) == 1 else None   # noqa: E731
        tok = one("t") or ""
        if not self.app.check_agents_token(tok, client=self.client_address[0]):
            return self.send(403, agents_result_page("denied"))
        lst, fp = self.app.assistants_state()
        csrf = self.app.agents_csrf(tok)
        values = {k: one(k) for k in ("agent_" + a for a in appmod.KNOWN_AGENTS) if k in q}
        if values:
            plan = agents_plan(lst, values)
            if plan is None:
                return self.send(200, agents_choose_page(tok, csrf, lst, appmod.ERR_AGENTS_LEVEL, values))
            return self.send(200, agents_confirm_page(tok, csrf, plan, fp))
        self.send(200, agents_choose_page(tok, csrf, lst))

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/assistants":
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        if not self._peer_host_ok():
            return
        if not same_origin(self.headers, self.origin):
            return self.send(403, agents_result_page("refused"))
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return self.send(415, agents_result_page("denied"))
        raw = self.read_body(MAX_FORM)
        if raw is None:
            return self.send(413, agents_result_page("denied"))
        try:
            q = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=8)   # t c action v + 3
        except (ValueError, UnicodeDecodeError):
            return self.send(400, agents_result_page("bad_request"))
        form = {k: v[0] for k, v in q.items() if len(v) == 1}
        code, kind, err, changed = self.app.agents_submit(form, client=self.client_address[0])
        ws = getattr(self.server, "webservers", None)
        if kind == "saved":
            page = agents_result_page("saved", self.app.assistants(), changed=changed)
        elif kind in ("cancelled", "unchanged"):
            page = agents_result_page(kind)
        elif kind == "form":
            tok = form.get("t", "")
            if code == 503:   # écriture impossible : politique en place conservée, nouvel essai possible
                page = agents_result_page("error", error=err)
            elif self.app.check_agents_token(tok):
                vals = {k: form.get(k) for k in ("agent_" + a for a in appmod.KNOWN_AGENTS)}
                page = agents_choose_page(tok, self.app.agents_csrf(tok), self.app.assistants(), err, vals)
            else:
                page = agents_result_page("denied")
        elif code == 429:
            page = agents_result_page("limited")
        else:
            page = agents_result_page("denied")
        self.send(code, page)
        if kind in ("saved", "cancelled", "unchanged") and ws is not None:
            # réponse déjà envoyée : la page /assistants se ferme maintenant (thread à part, jamais sous ce serveur)
            threading.Thread(target=ws.sync_lan, daemon=True, name="agents-close").start()


# --------------------------------------------------------------------------- télécommande sur téléphone (LAN)
PAD = ((None, ("up", "▲", "Haut"), None),
       (("left", "◀", "Gauche"), ("ok", "OK", "OK"), ("right", "▶", "Droite")),
       (None, ("down", "▼", "Bas"), None))


def _rpage(title, inner, script=False):
    # zoom laissé libre (accessibilité) ; referrer same-origin : les formulaires envoient l'Origin réelle
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"same-origin\"><meta name=\"color-scheme\" content=\"dark\">"
            "<title>%s</title><link rel=\"stylesheet\" href=\"/static/remote.css\">%s</head>"
            "<body><main>%s</main></body></html>"
            % (html.escape(title), "<script src=\"/static/remote.js\" defer></script>" if script else "", inner))


def remote_scan_page(name, error=None):
    """Téléphone non appairé : explication en 3 gestes, aucun champ, rien à recopier."""
    e = html.escape
    return _rpage("Télécommande OpenTV", (
        "<h1>Télécommande</h1>%s<p class=\"lead\">Pour piloter la TV «\u00a0%s\u00a0» avec ce téléphone :</p>"
        "<ol class=\"howto\"><li>Mets ce téléphone sur le <strong>même Wi-Fi que la TV</strong>.</li>"
        "<li>Ouvre l'appareil photo et vise le QR «\u00a0Télécommande\u00a0» à droite de l'écran OpenTV.</li>"
        "<li>Touche «\u00a0Oui, utiliser ce téléphone\u00a0».</li></ol>"
        % ("<p class=\"err\" role=\"alert\">%s</p>" % e(error) if error else "", e(name))))


def remote_confirm_page(name, secret):
    e = html.escape
    return _rpage("Télécommande OpenTV", (
        "<h1>Télécommande</h1><p class=\"lead\">Utiliser ce téléphone pour la TV «\u00a0%s\u00a0» ?</p>"
        "<form method=\"post\" action=\"/r/pair\"><input type=\"hidden\" name=\"p\" value=\"%s\">"
        "<button type=\"submit\" class=\"primary\">Oui, utiliser ce téléphone</button></form>"
        "<p class=\"hint\">Il restera connecté tant qu'il sert au moins une fois par mois. Pour l'oublier : bouton en "
        "bas du pavé, ou Réglages sur la TV.</p>"
        % (e(name), e(secret, quote=True))))


def remote_pad_page(name, csrf):
    e = html.escape
    cells = []
    for row in PAD:
        for b in row:
            cells.append("<span></span>" if b is None else
                         "<button type=\"submit\" name=\"a\" value=\"%s\" class=\"k %s\" aria-label=\"%s\">%s</button>"
                         % (b[0], b[0], e(b[2]), e(b[1])))
    c = e(csrf, quote=True)
    return _rpage("Télécommande OpenTV", (
        "<h1 class=\"tv\">%s</h1>"
        "<form method=\"post\" action=\"/r/cmd\" id=\"pad\"><input type=\"hidden\" name=\"c\" value=\"%s\">"
        "<div class=\"dpad\">%s</div><div class=\"row2\">"
        "<button type=\"submit\" name=\"a\" value=\"back\" class=\"k back\">↩ Retour</button>"
        "<button type=\"submit\" name=\"a\" value=\"home\" class=\"k home\">⌂ Accueil</button></div></form>"
        "<p id=\"status\" class=\"status\" role=\"status\" aria-live=\"polite\"></p>"
        "<p class=\"hint\">Astuce : ajoute cette page à l'écran d'accueil (bouton Partager, puis «\u00a0Sur l'écran "
        "d'accueil\u00a0») pour l'ouvrir comme une appli. Ce téléphone doit rester sur le même Wi-Fi que la TV.</p>"
        "<p class=\"hint\">Volume et marche/arrêt : télécommande de la TV (pas sur ce téléphone).</p>"
        "<form method=\"post\" action=\"/r/forget\" class=\"forget\"><input type=\"hidden\" name=\"c\" value=\"%s\">"
        "<button type=\"submit\">Oublier ce téléphone</button></form>"
        % (e(name), c, "".join(cells), c)), script=True)


def remote_message_page(title, text, link=True):
    e = html.escape
    return _rpage("Télécommande OpenTV", "<h1>%s</h1><p class=\"lead\">%s</p>%s" % (
        e(title), e(text), "<a class=\"primary\" href=\"/r\">Réessayer</a>" if link else ""))


def lan_peer(ip_text):
    """Pair accepté par défaut : IPv4 privée du réseau local, jamais la boucle locale ni le lien local."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or
                                                      ip.is_multicast)


class RemoteHandler(_Base):
    """Téléphone -> /r. Contrôles : pair IPv4 privé (hors boucle locale), Host = adresse LAN liée ou nom mDNS exact de
    la boîte, avec le port réel (DNS rebinding, Host étranger, loopback ou public refusés), POST seulement pour agir,
    Origin exactement égale à http://<Host> (« null » toujours refusé ; sans Origin : Sec-Fetch-Site/Referer
    cohérents), jeton CSRF par téléphone, cookie HttpOnly SameSite=Lax (Secure dès qu'un HTTPS propre à l'appareil
    existera). Referrer-Policy same-origin pour que les vrais navigateurs envoient l'Origin des formulaires."""
    csp = REMOTE_CSP
    referrer_policy = "same-origin"

    def _peer_host_ok(self):
        srv = self.server
        if not srv.peer_ok(self.client_address[0]):
            self.send(403, "interdit", "text/plain; charset=utf-8")
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.app.remote_hosts(srv.lan_ip, srv.server_address[1]):
            self.send(421, "hôte refusé", "text/plain; charset=utf-8")
            return False
        self.origin = "http://" + host
        return True

    def _guard(self):
        if not self._peer_host_ok():
            return False
        remote = self.app.remote
        if remote is None or not self.app.want_remote():
            self.send(503, remote_message_page("Télécommande indisponible",
                                               "La TV est en configuration ou hors ligne. Vérifie que ce téléphone "
                                               "est sur le même Wi-Fi que la TV, puis réessaie dans un moment."))
            return False
        self.remote = remote
        return True

    def _origin_ok(self):   # « null » (politique no-referrer, contexte opaque) n'est jamais égal : refus
        return same_origin(self.headers, self.origin)

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        if len(raw) > 4096:
            return None
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == REMOTE_COOKIE:
                return v.strip()
        return None

    def _set_cookie(self, tok):
        # HTTP LAN (prototype) : pas de « Secure » possible ; à ajouter avec le futur HTTPS propre à l'appareil
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (REMOTE_COOKIE, tok,
                                                                      int(phoneremote.SESSION_IDLE_S))

    def _clear_cookie(self):
        return "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % REMOTE_COOKIE

    def _wants_json(self):
        return "application/json" in (self.headers.get("Accept") or "")

    def _redirect(self, cookie=None):
        extra = {"Location": "/r"}
        if cookie:
            extra["Set-Cookie"] = cookie
        self.send(303, "", "text/plain; charset=utf-8", extra)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith("/static/"):   # mêmes contrôles de pair et de Host que les pages
            return self.send_static(u.path[len("/static/"):], REMOTE_STATIC) if self._peer_host_ok() else None
        if not self._guard():
            return
        if u.path == "/":
            return self._redirect()
        if u.path != "/r":
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        try:
            q = urllib.parse.parse_qs(u.query, max_num_fields=1)
        except ValueError:
            q = {}
        name = self.app.remote_device_name()
        phone = self.remote.session(self._cookie_token())
        if phone is not None:   # déjà appairé : le pavé (un ?p= éventuel n'est jamais consommé)
            return self.send(200, remote_pad_page(name, self.remote.csrf(phone)))
        p = (q.get("p") or [""])[0]
        if p:   # lecture seule : aucun appairage, aucun essai compté
            if self.remote.check_pair_secret(p):
                return self.send(200, remote_confirm_page(name, p))
            return self.send(403, remote_scan_page(name, "Ce QR a expiré ou a déjà servi : scanne celui affiché "
                                                         "maintenant sur la TV."))
        self.send(200, remote_scan_page(name))

    def _form(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return None
        raw = self.read_body(REMOTE_FORM_MAX)
        if raw is None:
            return None
        try:
            q = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
        except (ValueError, UnicodeDecodeError):
            return None
        return {k: v[0] for k, v in q.items() if len(v) == 1}

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/r/pair", "/r/cmd", "/r/forget"):
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        if not self._guard():
            return
        js = self._wants_json()
        if not self._origin_ok():
            return self.send_json(403, {"ok": False, "error": "origin"}) if js else \
                self.send(403, remote_message_page("Refusé", "Requête venue d'une autre page."))
        form = self._form()
        if form is None:
            return self.send_json(400, {"ok": False, "error": "bad_request"}) if js else \
                self.send(400, remote_message_page("Requête invalide", "Recharge la page."))
        ip = self.client_address[0]
        if path == "/r/pair":
            tok, err = self.remote.pair_phone(ip, form.get("p"))
            if tok:
                return self._redirect(self._set_cookie(tok))
            if err == "limited":
                return self.send(429, remote_message_page("Trop d'essais", "Attends quelques minutes puis scanne le "
                                                                           "QR affiché sur la TV."))
            return self.send(403, remote_scan_page(self.app.remote_device_name(),
                                                   "Ce QR a expiré ou a déjà servi : scanne celui affiché maintenant "
                                                   "sur la TV."))
        phone = self.remote.session(self._cookie_token())
        if phone is None:
            return self.send_json(401, {"ok": False, "error": "not_paired"}) if js else self._redirect()
        if not self.remote.check_csrf(phone, form.get("c")):   # jamais d'oubli : le pavé est simplement réaffiché
            return self.send_json(403, {"ok": False, "error": "csrf"}) if js else self._redirect()
        if path == "/r/forget":
            self.remote.forget(phone["id"])
            return self._redirect(self._clear_cookie())
        res = self.remote.command(phone, ip, form.get("a"))
        code = {"ok": 200, "limited": 429, "bad_action": 400}[res]
        if js:
            return self.send(code, json.dumps({"ok": res == "ok", "error": None if res == "ok" else res}),
                             "application/json; charset=utf-8")
        self._redirect()


class WebServers:
    """remote_bind / remote_peer_ok : coutures de test seulement (le service réel écoute sur l'adresse LAN elle-même
    et n'accepte que des pairs lan_peer)."""

    def __init__(self, app, bind_lan="0.0.0.0", remote_bind=None, remote_peer_ok=lan_peer, mono=time.monotonic):
        self.app, self.bind_lan = app, bind_lan
        self.remote_bind = remote_bind or (lambda ip: ip)
        self.remote_peer_ok = remote_peer_ok
        self.mono = mono
        self.remote_retry = (None, -1e9)   # (adresse, instant) du dernier échec d'écoute
        self.tv = self.lan = self.remote = None
        self.lock = threading.Lock()

    def _start(self, addr, handler, app, cls=_Server, **attrs):
        srv = cls(addr, handler)
        srv.app = app
        srv.webservers = self
        for k, v in attrs.items():
            setattr(srv, k, v)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        t.start()
        return srv

    @staticmethod
    def _stop(srv):
        if srv is not None:
            srv.shutdown()
            srv.server_close()

    def start_tv(self):
        self.tv = self._start(("127.0.0.1", self.app.cfg.tv_port), TVHandler, self.app)

    def sync_lan(self):
        """Écoute LAN : "setup" (configuration, bind_lan) ou "agents" (gestion des assistants : adresse LAN actuelle
        seulement, pages /assistants seulement). Changement de rôle ou d'adresse ⇒ écoute fermée puis rouverte ;
        jamais les deux rôles sur un même serveur."""
        want = self.app.lan_purpose()
        ip = self.app.lan_ip() if want == "agents" else None
        agents_failed = False
        with self.lock:
            cur = self.lan
            if cur is not None and (want != cur.purpose or (want == "agents" and cur.lan_ip != ip)):
                self._stop(cur)
                self.lan, self.app.lan_port_actual = None, None
                self.app.log("setup_listener", state="closed", purpose=cur.purpose)
            if want is not None and self.lan is None:
                try:
                    if want == "setup":
                        self.lan = self._start((self.bind_lan, self.app.cfg.setup_port), LANHandler, self.app,
                                               purpose="setup")
                    else:
                        self.lan = self._start((self.remote_bind(ip), self.app.cfg.setup_port), AgentsHandler,
                                               self.app, _PerIPServer, purpose="agents", lan_ip=ip,
                                               peer_ok=self.remote_peer_ok)
                    self.app.lan_port_actual = self.lan.server_address[1]
                    self.app.log("setup_listener", state="open", port=self.app.lan_port_actual, purpose=want)
                except OSError as e:
                    self.app.log("setup_listener_error", error=type(e).__name__, purpose=want)
                    agents_failed = want == "agents"
        if agents_failed:   # hors du verrou des écoutes : jamais de QR vide pendant 10 min, aucune boucle de bind
            self.app.agents_unavailable("bind")
        self.sync_remote()

    def sync_remote(self):
        """Télécommande téléphone : ouverte seulement configuré + en ligne (Ethernet/Wi-Fi), jamais en configuration,
        sur l'adresse LAN actuelle ; l'adresse change (DHCP, Ethernet ↔ Wi-Fi) ⇒ écoute relancée sur la nouvelle."""
        ip = self.app.remote_bind_ip()
        with self.lock:
            if self.remote is not None and self.remote.lan_ip != ip:
                self._stop(self.remote)
                self.remote = None
                self.app.remote_port_actual = self.app.remote_ip_actual = None
                self.app.log("remote_listener", state="closed", reason="address_changed" if ip else "off")
            if ip is None or self.remote is not None:
                return
            last_ip, at = self.remote_retry
            if last_ip == ip and self.mono() - at < REMOTE_RETRY_S:
                return
            try:
                srv = self._start((self.remote_bind(ip), self.app.cfg.remote_port), RemoteHandler,
                                              self.app, _PerIPServer, lan_ip=ip, peer_ok=self.remote_peer_ok)
            except OSError as e:
                self.remote_retry = (ip, self.mono())
                self.app.log("remote_listener_error", error=type(e).__name__)
                return
            self.remote, self.remote_retry = srv, (None, -1e9)
            self.app.remote_port_actual, self.app.remote_ip_actual = srv.server_address[1], ip
            self.app.log("remote_listener", state="open", port=self.app.remote_port_actual)

    def tv_port(self):
        return self.tv.server_address[1] if self.tv else None

    def lan_port(self):
        return self.lan.server_address[1] if self.lan else None

    def remote_port(self):
        return self.remote.server_address[1] if self.remote else None

    def stop(self):
        with self.lock:
            self._stop(self.lan)
            self._stop(self.remote)
            self.lan = self.remote = None
            self.app.remote_port_actual = self.app.remote_ip_actual = None
        self._stop(self.tv)
        self.tv = None
