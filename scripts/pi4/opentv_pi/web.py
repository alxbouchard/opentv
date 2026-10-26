"""Serveurs HTTP du service (stdlib).

- TV (127.0.0.1 seulement, toujours) : /tv, /static/*, /api/tv/* (en-tête secret par démarrage, Host vérifié contre le
  DNS rebinding, JSON borné). CSP stricte : aucun script en ligne, connexions vers soi uniquement.
- LAN (0.0.0.0:OPENTV_SETUP_PORT, seulement pendant la configuration) : /setup (formulaire sans JavaScript) et
  /static/setup.css. Rien d'autre. Le jeton n'apparaît jamais dans un journal.
"""
import hmac
import html
import http.server
import ipaddress
import json
import os
import threading
import time
import urllib.parse

from . import VERSION, bounded

TV_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
          "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SETUP_CSP = "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
TV_STATIC = {"tv.js": "application/javascript; charset=utf-8", "tv.css": "text/css; charset=utf-8",
             "qrcode.js": "application/javascript; charset=utf-8", "setup.css": "text/css; charset=utf-8"}
MAX_JSON = 2048
TV_KEY_ERROR = "tv_key"   # 403 de /api/tv/* : clé de page périmée (service redémarré) -> tv.js recharge /tv, borné
MAX_FORM = 4096
AGENT_LABELS = (("muse", "Muse"), ("hermit", "Hermit"), ("hermes", "Hermes"))
LEVEL_LABELS = (("none", "Aucun accès"), ("read", "Lecture seule"), ("feedback", "Lecture + avis"),
                ("full", "Lecture + avis + messages sur la TV"))


# Journal HTTP : erreurs et non-2xx toujours ; navigations de page (/tv, /setup) toujours (rares, bornées) ; succès
# des requêtes répétitives (poll /api/tv/*, /static/*, /healthz) non journalisés, un échantillon agrégé tous les N.
QUIET_OK_PREFIXES = ("/api/tv/", "/static/")
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


class _Base(http.server.BaseHTTPRequestHandler):
    server_version = "OpenTV-Pi"
    sys_version = ""
    timeout = 10
    csp = TV_CSP

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
        self.send_header("Referrer-Policy", "no-referrer")
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
        self.send_json(404, {"ok": False, "error": "not_found"})


# --------------------------------------------------------------------------- page de configuration (téléphone)
def _page(title, inner):
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><title>%s</title>"
            "<link rel=\"stylesheet\" href=\"/static/setup.css\"></head><body><main>%s</main></body></html>"
            % (html.escape(title), inner))


def setup_form(token, ctx, error=None, values=None):
    e, v = html.escape, values or {}
    online = ctx["net_mode"] in ("ethernet", "wifi")
    net = v.get("net") or ("keep" if online else "wifi")
    parts = ["<h1>Configurer OpenTV</h1>"]
    if error:
        parts.append("<p class=\"err\" role=\"alert\">%s</p>" % e(error))
    parts.append("<form method=\"post\" action=\"/setup\" autocomplete=\"off\">")
    parts.append("<input type=\"hidden\" name=\"t\" value=\"%s\">" % e(token))
    parts.append("<label>Nom de la TV<input name=\"name\" maxlength=\"32\" required value=\"%s\" "
                 "placeholder=\"Salon\"></label>" % e(v.get("name") or ctx.get("name") or "Salon"))
    parts.append("<fieldset><legend>Connexion</legend>")
    if online:
        parts.append("<label class=\"opt\"><input type=\"radio\" name=\"net\" value=\"keep\"%s> Garder la connexion "
                     "actuelle (%s)</label>" % (" checked" if net == "keep" else "",
                                                "câble Ethernet" if ctx["net_mode"] == "ethernet" else "Wi-Fi"))
    parts.append("<label class=\"opt\"><input type=\"radio\" name=\"net\" value=\"wifi\"%s> Connecter au Wi-Fi</label>"
                 % (" checked" if net == "wifi" else ""))
    parts.append("<label>Nom du Wi-Fi<input name=\"ssid\" maxlength=\"32\" value=\"%s\"></label>" % e(v.get("ssid") or ""))
    parts.append("<label>Mot de passe Wi-Fi<input name=\"psk\" type=\"password\" maxlength=\"64\" "
                 "autocomplete=\"new-password\"></label>")
    parts.append("<label class=\"opt\"><input type=\"checkbox\" name=\"hidden\" value=\"1\"> Réseau masqué</label>")
    parts.append("</fieldset><fieldset><legend>Agents autorisés sur cette TV</legend>")
    pol = ctx.get("policy") or {}
    for aid, label in AGENT_LABELS:
        cur = v.get("agent_" + aid)
        if cur is None:
            sc = set(pol.get(aid, []))
            cur = "full" if "display" in sc else "feedback" if "feedback" in sc else "read" if "read" in sc else "none"
        opts = "".join("<option value=\"%s\"%s>%s</option>" % (k, " selected" if k == cur else "", e(t))
                       for k, t in LEVEL_LABELS)
        parts.append("<label>%s<select name=\"agent_%s\">%s</select></label>" % (e(label), aid, opts))
    parts.append("<p class=\"hint\">« Aucun accès » est enregistré comme un refus. L'accès d'un agent s'active ensuite "
                 "sur la boîte (<code>opentv-pi agent issue</code>), dans la limite choisie ici. Aucun agent ne reçoit "
                 "de mot de passe ni d'adresse.</p>")
    parts.append("</fieldset><button type=\"submit\">Enregistrer</button></form>")
    parts.append("<p class=\"hint\">Le mot de passe Wi-Fi est remis seulement à NetworkManager sur la boîte ; "
                 "il n'est jamais affiché ni journalisé.</p>")
    return _page("Configurer OpenTV", "".join(parts))


def setup_result(kind):
    if kind == "applying":
        return _page("OpenTV", "<h1>C'est prêt !</h1><p>Regarde la TV : elle confirme la connexion au Wi-Fi. "
                               "Si le mot de passe est refusé, un nouveau QR s'affichera.</p>")
    if kind == "done":
        return _page("OpenTV", "<h1>C'est prêt !</h1><p>OpenTV s'affiche maintenant sur la TV. "
                               "Tu peux fermer cette page.</p>")
    if kind == "bad_request":
        return _page("OpenTV", "<h1>Adresse invalide</h1><p>Scanne de nouveau le QR affiché sur la TV.</p>")
    return _page("OpenTV", "<h1>Code expiré ou déjà utilisé</h1><p>Scanne le nouveau QR affiché sur la TV.</p>")


class LANHandler(_Base):
    csp = SETUP_CSP

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/static/setup.css":
            return self.send_static("setup.css", {"setup.css": TV_STATIC["setup.css"]})
        if u.path != "/setup":
            return self.send(404, "introuvable", "text/plain; charset=utf-8")
        try:   # requête trop large ou malformée : 400 borné, sans trace ; le jeton n'est jamais consommé par un GET
            q = urllib.parse.parse_qs(u.query, max_num_fields=4)
        except ValueError:
            return self.send(400, setup_result("bad_request"))
        tok = (q.get("t") or [""])[0]
        if not self.app.check_token(tok):
            return self.send(403, setup_result("denied"))
        self.send(200, setup_form(tok, self.app.setup_context()))

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
        code, kind, err = self.app.setup_submit(form, client=self.client_address[0])
        if kind == "form":
            vals = {k: form.get(k) for k in ("name", "net", "ssid", "agent_muse", "agent_hermit", "agent_hermes")}
            return self.send(code, setup_form(form.get("t", ""), self.app.setup_context(), err, vals))
        self.send(code, setup_result(kind))


class WebServers:
    def __init__(self, app, bind_lan="0.0.0.0"):
        self.app, self.bind_lan = app, bind_lan
        self.tv = self.lan = None
        self.lock = threading.Lock()

    @staticmethod
    def _start(addr, handler, app):
        srv = _Server(addr, handler)
        srv.app = app
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
        want = self.app.want_lan()
        with self.lock:
            if want and self.lan is None:
                try:
                    self.lan = self._start((self.bind_lan, self.app.cfg.setup_port), LANHandler, self.app)
                    self.app.lan_port_actual = self.lan.server_address[1]
                    self.app.log("setup_listener", state="open", port=self.app.lan_port_actual)
                except OSError as e:
                    self.app.log("setup_listener_error", error=type(e).__name__)
            elif not want and self.lan is not None:
                self._stop(self.lan)
                self.lan, self.app.lan_port_actual = None, None
                self.app.log("setup_listener", state="closed")

    def tv_port(self):
        return self.tv.server_address[1] if self.tv else None

    def lan_port(self):
        return self.lan.server_address[1] if self.lan else None

    def stop(self):
        with self.lock:
            self._stop(self.lan)
            self.lan = None
        self._stop(self.tv)
        self.tv = None
