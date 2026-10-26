"""Serveurs HTTP du service (stdlib).

- TV (127.0.0.1 seulement, toujours) : /tv, /static/*, /api/tv/* (en-tête secret par démarrage, Host vérifié contre le
  DNS rebinding, JSON borné). CSP stricte : aucun script en ligne, connexions vers soi uniquement.
- LAN (0.0.0.0:OPENTV_SETUP_PORT, seulement pendant la configuration) : /setup (assistant en 3 étapes, utilisable
  sans JavaScript), /static/setup.css et /static/setup.js. Rien d'autre. Le jeton n'apparaît jamais dans un journal.
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

from . import VERSION, bounded, validate, wifiscan
from . import app as appmod   # constantes d'erreur du formulaire (app n'importe pas web : pas de cycle)

TV_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
          "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# Page du téléphone : un seul script externe de même origine (afficher le mot de passe, double envoi), aucun script
# en ligne, aucune connexion (connect-src hérite de default-src 'none'), formulaires vers soi uniquement.
SETUP_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; "
             "base-uri 'none'; frame-ancestors 'none'")
TV_STATIC = {"tv.js": "application/javascript; charset=utf-8", "tv.css": "text/css; charset=utf-8",
             "qrcode.js": "application/javascript; charset=utf-8", "setup.css": "text/css; charset=utf-8"}
SETUP_STATIC = {"setup.css": "text/css; charset=utf-8", "setup.js": "application/javascript; charset=utf-8"}
MAX_JSON = 2048
TV_KEY_ERROR = "tv_key"   # 403 de /api/tv/* : clé de page périmée (service redémarré) -> tv.js recharge /tv, borné
MAX_FORM = 4096
AGENT_LABELS = (("muse", "Muse"), ("hermit", "Hermit"), ("hermes", "Hermes"))
LEVEL_LABELS = (("none", "Aucun accès"), ("read", "Voir seulement"), ("feedback", "Voir + donner mon avis"),
                ("full", "Tout : avis + messages sur la TV"))


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


def _level_of(scopes):
    sc = set(scopes or ())
    return "full" if "display" in sc else "feedback" if "feedback" in sc else "read" if "read" in sc else "none"


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
        "%s<p class=\"hint\">Aucun agent n'a accès par défaut. Un accès choisi ici s'active ensuite sur la boîte "
        "(<code>opentv-pi agent issue</code>). Aucun agent ne reçoit de mot de passe.</p></details>"
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


def setup_result(kind):
    if kind == "applying":
        return _page("OpenTV", _progress(3) + "<h1>C'est presque fini !</h1>"
                     "<p class=\"lead\">Regarde la TV : elle se connecte à ton Wi-Fi (moins d'une minute).</p>"
                     "<p>Si le mot de passe est refusé, la TV affiche un nouveau QR : scanne-le pour réessayer.</p>"
                     "<p class=\"hint\">Ton téléphone peut perdre le Wi-Fi «\u00a0OpenTV\u00a0» : c'est normal.</p>")
    if kind == "done":
        return _page("OpenTV", _progress(3) + "<h1>C'est prêt !</h1>"
                     "<p class=\"lead\">OpenTV s'affiche maintenant sur la TV.</p><p>Tu peux fermer cette page.</p>")
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
        code, kind, err = self.app.setup_submit(form, client=self.client_address[0])
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
