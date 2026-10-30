"""OpenTV Canvas (tâche 3) : rendu réel sur la page /tv, Réglages « Ambiance OpenTV » et cycle de vie du navigateur.

Statique (toujours) : canvas.js ES5 (motifs + Acorn réel si OPENTV_TEST_ACORN), miroir exact du schéma Python, aucune
requête/HTML/style libre ; tv.css : animations continues transform/opacity seulement sur au plus quatre couches,
aucun filtre/flou/backdrop/will-change/ressource ; la scène ne stylise jamais la coque (Réglages, confirmations,
boutons, QR, lecteur, hors-ligne) ; tv.html : couches fixes sous le voile, feuille Canvas hors du panneau, ordre des
scripts ; service : canvas.js servi par la liste blanche, CSP inchangée.
Navigateur réel 1920×1080 (OPENTV_TEST_PLAYWRIGHT) : `e2e_canvas_ui.js` (API simulée, réponses retardées, perdues ou
désordonnées) et `e2e_canvas_service.js` (vrai service, vrai magasin Canvas, Agent simulé par canal fichier).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from tests._common import PI4, ROOT
from tests.test_service import Servers, find_chromium, make_app
from opentv_pi import canvas as cv, canvas_tv, config, web

UI = os.path.join(PI4, "ui")
CANVAS_JS = os.path.join(UI, "canvas.js")
TV_JS = os.path.join(UI, "tv.js")
TV_CSS = os.path.join(UI, "tv.css")
TV_HTML = os.path.join(UI, "tv.html")
QRCODE_JS = os.path.join(ROOT, "native", "app", "qrcode.js")
NODE = shutil.which("node")
ES6 = re.compile(r"(?<![\w$.])(let|const|class)\s|=>|`|\.\.\.[A-Za-z_$\[]|\bfor\s*\([^)]*\bof\b")
CANVAS_MARK = "/* ==== OpenTV Canvas (canvas.js)"
SHELL_TOKENS = ("panel", "pbtns", "sheet", "lrow", "remotebox", "rqr", "qbtn", "gear", "glabel", "setup", "toast",
                "offline", "playov", "cvsheet", "canvas", "button", "qrs", "figure", "pqr", "aqr", "help", "hint",
                "status", "badge", "banner", "clock")
ANIMATED = {"cv-l1", "cv-l2", "cv-l3", "cvglow"}


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def strip_js_comments(code):
    return "\n".join(x for x in code.splitlines() if not x.lstrip().startswith(("//", "*", "/*")))


def canvas_css():
    css = read(TV_CSS)
    i = css.index(CANVAS_MARK)
    return re.sub(r"/\*.*?\*/", "", css[i:], flags=re.S)


def css_rules(css):
    """(sélecteur, déclarations) des règles simples ; @keyframes et @media aplatis à part."""
    css = re.sub(r"@keyframes\s+[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*\s*\}", "", css)
    css = re.sub(r"@media[^{]*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", r"\1", css)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def playwright_dir():
    p = os.environ.get("OPENTV_TEST_PLAYWRIGHT")
    return p if p and os.path.isfile(os.path.join(p, "package.json")) else None


def acorn_dir():
    p = os.environ.get("OPENTV_TEST_ACORN")
    return p if p and os.path.isfile(os.path.join(p, "package.json")) else None


# --------------------------------------------------------------------------- statique
class TestCanvasJsStatic(unittest.TestCase):
    def test_es5_tokens(self):
        for f in (CANVAS_JS, TV_JS):
            code = strip_js_comments(read(f))
            m = ES6.search(code)
            self.assertIsNone(m, (f, m and code[max(0, m.start() - 40):m.end() + 20]))

    @unittest.skipUnless(NODE and acorn_dir(), "acorn absent (OPENTV_TEST_ACORN=<chemin du module>)")
    def test_acorn_ecma5(self):
        code = ("var a=require(process.argv[1]),fs=require('fs');"
                "process.argv.slice(2).forEach(function(f){a.parse(fs.readFileSync(f,'utf8'),{ecmaVersion:5});});"
                "process.stdout.write('acorn-ok');")
        p = subprocess.run([NODE, "-e", code, acorn_dir(), CANVAS_JS, TV_JS], capture_output=True, timeout=60)
        self.assertEqual((p.returncode, p.stdout), (0, b"acorn-ok"), p.stderr.decode())

    def test_enums_rows_and_default_mirror_python_schema(self):
        js = read(CANVAS_JS)
        enums = json.loads(re.search(r"var ENUMS = (\{.*?\});", js).group(1))
        self.assertEqual(enums, {k: list(v) for k, v in cv.ENUMS.items()})
        self.assertEqual(json.loads(re.search(r"var ROW_IDS = (\[.*?\]);", js).group(1)), list(cv.ROW_IDS))
        self.assertEqual(json.loads(re.search(r"var DEFAULT = (\{.*?\});", js).group(1)), cv.DEFAULT_SCENE.to_dict())
        self.assertEqual(json.loads(re.search(r"var KEYS = (\[.*?\]);", js).group(1)), list(cv.SCENE_KEYS))
        self.assertIn('var FIRST_ROW = "%s";' % cv.FIRST_ROW, js)

    def test_renderer_never_fetches_injects_or_styles_freely(self):
        low = strip_js_comments(read(CANVAS_JS)).lower()
        for bad in ("fetch(", "xmlhttprequest", "websocket", "worker(", "eventsource", "sendbeacon", "innerhtml",
                    "outerhtml", "insertadjacenthtml", "document.write", "eval(", "new function", "setattribute(\"style",
                    ".style.", ".style=", "cssText", "url(", "http", "import(", "localstorage", "sessionstorage",
                    "<video", "createelement(\"video", "createelement(\"img", ".src", "filter", "blur",
                    "settimeout(\"", "setinterval("):
            self.assertNotIn(bad.lower(), low, bad)
        # seules des classes construites à partir des énumérations fermées sont posées
        for m in re.finditer(r"className\s*=\s*([^;]+);", strip_js_comments(read(CANVAS_JS))):
            self.assertRegex(m.group(1), r"^(cls\.join\(\" \"\)|\"cv-g \" \+ .*|c\.className\.replace\(.*\))$", m.group(0))
        # tv.js : aucune propriété de style alimentée par la scène (seuls le visuel dessiné et les QR existants)
        tvjs = strip_js_comments(read(TV_JS))
        m = re.search(r"\.style\.[a-zA-Z]+\s*=\s*[^;]*(cvPub|cvSheet|scene\b|state\.canvas|\.wallpaper|\.accent)", tvjs)
        self.assertIsNone(m, m and m.group(0))

    def test_deadlines_use_monotonic_clock(self):
        code = strip_js_comments(read(TV_JS))
        self.assertIn('typeof window.performance.now === "function"', code)
        for fn in ("openSheet", "tickSheet"):
            body = re.search(r"function %s\([^)]*\) \{(.*?)\n  \}" % fn, code, re.S).group(1)
            self.assertIn("mono()", body, fn)
            self.assertNotIn("Date.now", body, fn)


class TestCanvasCssStatic(unittest.TestCase):
    def test_keyframes_and_canvas_transitions_only_transform_opacity(self):
        css = re.sub(r"/\*.*?\*/", "", read(TV_CSS), flags=re.S)
        kfs = re.findall(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)
        self.assertGreaterEqual(len(kfs), 7)
        for name, body in kfs:   # toutes les animations continues de la page, coque comprise
            for decl in re.findall(r"\{([^{}]*)\}", body):
                for prop in re.findall(r"([\w-]+)\s*:", decl):
                    self.assertIn(prop, ("transform", "opacity"), name)
        for m in re.finditer(r"transition(?:-property)?\s*:\s*([^;}]+)", canvas_css()):
            for part in m.group(1).split(","):
                self.assertIn(part.strip().split()[0], ("transform", "opacity", "none"), m.group(0))

    def test_no_forbidden_features(self):
        css = re.sub(r"/\*.*?\*/", "", read(TV_CSS), flags=re.S)
        low = css.lower()
        for bad in ("backdrop-filter", "filter:", "blur(", "will-change", "@import", "@font-face", "image-set(",
                    "-webkit-mask", "mask-image", "mix-blend-mode"):
            self.assertNotIn(bad, low, bad)
        urls = re.findall(r"url\(\s*\"?([^\")]{0,30})", css)
        self.assertEqual(len(urls), 1, urls)                         # seule la photo v23 embarquée
        self.assertTrue(urls[0].startswith("data:image/jpeg"), urls)
        self.assertNotIn("http", canvas_css())

    def test_at_most_four_animated_layers(self):
        layers = set()
        for sel, decl in css_rules(canvas_css()):
            m = re.search(r"(?:^|;)\s*animation\s*:\s*([^;]+)", decl)
            if not m or m.group(1).strip().startswith("none"):
                continue
            for part in sel.split(","):
                last = re.split(r"\s+", part.strip())[-1]
                layers.update(x for x in re.findall(r"[.#]([\w-]+)", last))
        self.assertTrue(layers, "aucune couche animée")
        self.assertLessEqual(layers, ANIMATED)
        self.assertLessEqual(len(layers), 4)
        for sel, decl in css_rules(canvas_css()):   # durées : lentes (ambiance), jamais une boucle rapide
            for d in re.findall(r"animation(?:-duration)?\s*:[^;]*?([\d.]+)s", decl):
                self.assertGreaterEqual(float(d), 20, (sel, decl))
        self.assertIn("prefers-reduced-motion", canvas_css())
        self.assertRegex(canvas_css(), r"\.cv-paused #cvwall > div \{ animation-play-state: paused; \}")
        self.assertRegex(canvas_css(), r"\.cv-static #cvwall > div \{ animation: none; \}")

    def test_scene_never_styles_the_shell(self):
        """Règles de la scène (préfixe de classe « cv- » de <body>) : jamais Réglages, confirmations, boutons, QR,
        configuration, lecteur, hors-ligne ni toasts ; la feuille Canvas elle-même n'a que des styles de coque."""
        for sel, _ in css_rules(canvas_css()):
            for part in sel.split(","):
                part = part.strip()
                scene = re.search(r"\.cv(?:-[\w]+)*\b", part) and not part.startswith((".cvchanges", ".cvcount"))
                if not scene:
                    continue
                for tok in re.findall(r"[.#]([\w-]+)", part):
                    for bad in SHELL_TOKENS:
                        self.assertFalse(tok == bad or tok.startswith(bad + "-") or tok == "cv" + bad,
                                         (part, tok))
        for sel, _ in css_rules(canvas_css()):   # la feuille (coque) ne dépend d'aucune classe de scène
            if "cvchanges" in sel or "cvcount" in sel:
                self.assertNotRegex(sel, r"\.cv-|\.cv\b", sel)


class TestTvHtmlStatic(unittest.TestCase):
    def test_layers_sheet_and_scripts(self):
        html = read(TV_HTML)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")         # aucun script en ligne
        self.assertNotRegex(html, r"<style|style=")
        scripts = re.findall(r'<script src="([^"]+)"', html)
        self.assertEqual(scripts, ["/static/qrcode.js", "/static/keys.js", "/static/canvas.js", "/static/tv.js"])
        bg = re.search(r'<div id="bg"[^>]*>(.*?)\n  </div>', html, re.S).group(1)
        self.assertLess(bg.index('id="cvwall"'), bg.index('id="bgShade"'))   # couches SOUS le voile
        for cls in ("cv-base", "cv-l cv-l1", "cv-l cv-l2", "cv-l cv-l3"):
            self.assertIn('class="%s"' % cls, bg)
        self.assertIn('<div id="cvglow"><div id="cvga" class="cv-g"></div><div id="cvgb" class="cv-g"></div></div>', bg)
        panel = re.search(r'<div id="panel".*?\n  </div>', html, re.S).group(0)
        self.assertNotIn("cvsheet", panel)                               # la feuille Canvas n'est pas un panneau
        self.assertIn('<div id="cvsheet" class="panel sheet cvsheet" role="dialog" hidden>', html)
        self.assertNotRegex(html.lower(), r"https?://")

    def test_static_allowlist_and_csp(self):
        self.assertEqual(web.TV_STATIC["canvas.js"], "application/javascript; charset=utf-8")
        self.assertEqual(set(web.TV_STATIC), {"tv.js", "keys.js", "canvas.js", "tv.css", "qrcode.js", "setup.css"})
        self.assertEqual(web.TV_CSP, "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: "
                                     "https:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                     "frame-ancestors 'none'")

    def test_every_tv_static_file_is_installed_and_in_manifest(self):
        """Installation réelle dans une racine temporaire : chaque fichier servi par /static/ (canvas.js compris) est
        livré à l'identique et figure au manifeste (empreinte de livraison de la promotion terrain)."""
        r = tempfile.mkdtemp(prefix="otvcv-inst")
        self.addCleanup(shutil.rmtree, r, True)
        p = subprocess.run(["bash", os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"],
                           capture_output=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-2000:] + p.stderr.decode()[-2000:])
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), encoding="utf-8") as f:
            man = f.read()
        for name in web.TV_STATIC:
            dst = os.path.join(r, "opt/opentv/share/ui", name)
            self.assertTrue(os.path.isfile(dst), name)
            self.assertIn("/opt/opentv/share/ui/" + name, man, name)
            if name != "qrcode.js":
                with open(dst, "rb") as a, open(os.path.join(UI, name), "rb") as b:
                    self.assertEqual(a.read(), b.read(), name)

    def test_canvas_js_served_by_tv_handler_only(self):
        a = make_app(self)
        s = Servers(self, a)
        code, h, body = s.req("GET", "/static/canvas.js")
        self.assertEqual(code, 200)
        self.assertTrue(h["content-type"].startswith("application/javascript"))
        self.assertIn("window.OpenTVCanvas", body)
        self.assertEqual(h["content-security-policy"], web.TV_CSP)
        self.assertEqual(s.req("GET", "/static/canvas.js", lan=True)[0], 404)   # jamais sur le réseau local
        self.assertEqual(s.req("GET", "/static/lab.html")[0], 404)


@unittest.skipUnless(NODE, "node absent")
class TestCanvasJsNode(unittest.TestCase):
    """canvas.js exécuté dans node avec un DOM minimal : validation fermée, libellés, description de la feuille."""
    RUN = r"""
var vm=require('vm'),fs=require('fs');
function E(id){return {id:id,className:'',hidden:false,textContent:'',attrs:{},setAttribute:function(k,v){this.attrs[k]=v;}};}
var els={};['cvpill','cvga','cvgb'].forEach(function(i){els[i]=E(i);});
var body=E('body');var ctx={window:{requestAnimationFrame:function(){return 1;},cancelAnimationFrame:function(){},
 matchMedia:function(){return {matches:false,addListener:function(){}};}},
 document:{body:body,hidden:false,getElementById:function(i){return els[i]||null;},addEventListener:function(){}},
 setTimeout:function(f){f();return 1;},clearTimeout:function(){},Object:Object,JSON:JSON,Math:Math,isFinite:isFinite};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
var C=ctx.window.OpenTVCanvas,cases=JSON.parse(process.argv[2]),out={};
out.valid=cases.valid.map(function(s){return C.valid(s);});
out.describe=C.describe(cases.changes);
out.labels=[C.label('wallpaper','aurore'),C.label('wallpaper','x'),C.label('constructor','x'),C.label('motion','__proto__')];
out.applyBad=C.apply({wallpaper:'url(https://evil)'},{on:true});out.bodyBad=body.className;
out.applyOk=C.apply(cases.valid[0],{on:true,preview:true});out.bodyOk=body.className;out.pill=els.cvpill.textContent;
C.apply(cases.valid[0],{on:true,setup:true});out.bodySetup=body.className;
C.apply(null,{on:false});out.bodyOff=body.className;
C.focus(200);out.glow=[els.cvga.className,els.cvgb.className];
C.apply(cases.valid[0],{on:true,preview:true});C.pause([]);out.runMode=C.state().mode;
C.pause(['sheet']);out.sheet=[C.state().mode,C.state().reasons,body.className.indexOf('cv-paused')>=0];
C.pause([]);out.afterSheet=C.state().mode;
out.frozen=Object.isFrozen(C);
process.stdout.write(JSON.stringify(out));
"""

    def run_js(self, cases):
        p = subprocess.run([NODE, "-e", self.RUN, CANVAS_JS, json.dumps(cases)], capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return json.loads(p.stdout)

    def test_closed_validation_labels_and_description(self):
        good = [sc.to_dict() for sc in cv.PRESETS.values()]
        bad = []
        for k in cv.SCENE_KEYS:
            d = dict(good[0]); d.pop(k); bad.append(d)                        # clé manquante
        for k, vals in cv.ENUMS.items():
            for v in ("", "x", "https://evil.example/a.png", "<b>", None, 1, ["aurore"]):
                d = dict(good[0]); d[k] = v; bad.append(d)
        d = dict(good[0]); d["css"] = "body{}"; bad.append(d)                 # clé inconnue
        d = dict(good[0]); d["schema_version"] = 2; bad.append(d)
        for rows in (["new", "tonight", "watchlist", "top", "more"], ["tonight", "tonight", "new", "top", "more"],
                     ["tonight", "new", "top", "more"], ["tonight", "watchlist", "new", "top", "more", "x"], "tonight"):
            d = dict(good[0]); d["row_order"] = rows; bad.append(d)
        bad += [None, [], "aurore", 1]
        changes = [{"field": "wallpaper", "from": "aurore", "to": "affiche"}, {"field": "motion", "from": "calm", "to": "alive"},
                   {"field": "accent", "from": "ice", "to": "<script>"}, {"field": "css", "from": "a", "to": "b"},
                   {"field": "row_order", "from": [], "to": ["tonight", "top", "new", "watchlist", "more"]}, "x"]
        out = self.run_js({"valid": good + bad, "changes": changes})
        self.assertEqual(out["valid"], [True] * len(good) + [False] * len(bad))
        self.assertEqual(out["describe"], [{"label": "Fond", "from": "Aurore", "to": "Affiche"},
                                           {"label": "Mouvement", "from": "Calme", "to": "Vivant"},
                                           {"label": "Ordre des rangées", "from": None,
                                            "to": "Le choix de ce soir, Les mieux notés, Nouveautés…"}])
        self.assertEqual(out["labels"], ["Aurore", None, None, None])
        self.assertFalse(out["applyBad"])                                    # scène invalide ⇒ ambiance d'origine
        self.assertIn("cv-wp-aurore", out["bodyBad"])
        self.assertNotIn("evil", out["bodyBad"])
        self.assertTrue(out["applyOk"])
        self.assertIn("cv-preview", out["bodyOk"])
        self.assertEqual(out["pill"], "Aperçu de l'ambiance")
        self.assertEqual(out["bodySetup"], "cv-setup")                     # configuration : v23 intact
        self.assertEqual(out["bodyOff"], "")
        self.assertEqual(out["glow"][1], "cv-g cv-h6 on")                  # teinte 200° -> classe fermée
        self.assertTrue(out["frozen"])
        self.assertEqual(out["runMode"], "run")
        self.assertEqual(out["sheet"], ["paused", ["sheet"], True])        # feuille Canvas : couches figées
        self.assertEqual(out["afterSheet"], "run")


# --------------------------------------------------------------------------- navigateur réel : API simulée
def run_node(tc, script, cfg, timeout=600):
    p = subprocess.run([NODE, os.path.join(PI4, "tests", script), playwright_dir(), json.dumps(cfg)],
                       capture_output=True, timeout=timeout)
    tc.assertEqual(p.returncode, 0, p.stderr.decode()[-4000:])
    return json.loads(p.stdout.decode().strip().splitlines()[-1])


def shots_dir(tc, name):
    base = os.environ.get("OPENTV_CANVAS_SHOTS")
    if base:
        d = os.path.join(base, name)
        os.makedirs(d, exist_ok=True)
        return d
    d = tempfile.mkdtemp(prefix="otvcv", dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    return d


@unittest.skipUnless(NODE and playwright_dir() and find_chromium(),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
class TestCanvasUiBrowser(unittest.TestCase):
    """Vraie page /tv (tv.html, tv.css, canvas.js, keys.js, tv.js du dépôt) 1920×1080 sous la CSP du service ; API
    simulée côté Playwright pour piloter retards, pertes et désordre des réponses."""

    @classmethod
    def setUpClass(cls):
        cls.shots = shots_dir(cls, "ui") if os.environ.get("OPENTV_CANVAS_SHOTS") else \
            tempfile.mkdtemp(prefix="otvcvui", dir="/tmp")
        cfg = {"chromium": find_chromium(), "ui": UI, "qrcode": QRCODE_JS, "csp": web.TV_CSP, "shots": cls.shots,
               "presets": {k: v.to_dict() for k, v in cv.PRESETS.items()}}
        p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_canvas_ui.js"), playwright_dir(), json.dumps(cfg)],
                           capture_output=True, timeout=900)
        cls.stderr = p.stderr.decode()[-6000:]
        cls.code = p.returncode
        cls.res = json.loads(p.stdout.decode().strip().splitlines()[-1]) if p.returncode == 0 else None

    def setUp(self):
        self.assertEqual(self.code, 0, self.stderr)

    def r(self, key):
        self.assertIn(key, self.res)
        return self.res[key]

    def test_load_declares_empty_busy_then_renders_default(self):
        x = self.r("load")
        self.assertEqual(x["firstCanvasPost"], {"p": "/api/tv/canvas/ui", "b": {"busy": []}})
        self.assertEqual(x["mode"], "run")
        for c in ("cv", "cv-wp-aurore", "cv-acc-ice", "cv-shade-balanced", "cv-hero-title", "cv-m-calm", "cv-anim"):
            self.assertIn(c, x["body"].split())
        self.assertEqual(x["external"], [])                               # aucune ressource distante
        self.assertEqual(x["pill"], "Ce soir pour toi")
        self.assertEqual(x["whyChip"]["cls"], "chip why")

    def test_event_posted_before_every_ordinary_key_acts(self):
        x = self.r("order")
        self.assertGreaterEqual(len([k for k in x["keys"] if k["acted"]]), 6)
        for k in x["keys"]:
            self.assertTrue(k["event_first"], k)
            self.assertEqual(k["kind"], "key", k)
        self.assertLess(x["delayed_focus_ms"], 250)                        # sans aperçu : jamais d'attente
        self.assertEqual(x["busy_after_panel"], ["panel"])
        self.assertEqual(x["busy_after_close"], [])

    def test_known_preview_gates_key_until_cancel_is_established(self):
        x = self.r("gate")
        self.assertIn("cv-wp-aurore", x["bodyNoSheet"].split())             # aperçu jamais peint sans la feuille
        self.assertNotIn("cv-preview", x["bodyNoSheet"].split())
        self.assertEqual(x["before"], x["at100"])                          # pas d'action avant l'annulation
        self.assertNotEqual(x["before"], x["after"])                       # puis la touche agit
        self.assertEqual(x["failed_after"], x["failed_before"])           # échec : touche consommée
        self.assertEqual(x["slow_after"], x["slow_before"])                # délai dépassé : consommée, sans gel
        self.assertNotEqual(x["slow_next"], x["slow_after"])               # touche suivante : normale
        self.assertFalse(x["sheet_seen"])

    def test_stale_state_never_opens_sheet_after_human_key(self):
        x = self.r("stale")
        self.assertFalse(x["sheet_seen"])
        self.assertTrue(x["preview_was_served"])

    def test_sheet_lifecycle_no_and_back(self):
        x = self.r("sheet")
        self.assertTrue(x["shown"])
        self.assertEqual(x["focus"], "Non, garder l'ambiance actuelle")
        self.assertEqual(x["kicker"], "Proposé par Muse")
        self.assertEqual(x["changes"][0], "Fond : Aurore → Affiche")
        self.assertIn("cv-preview", x["bodyShown"])
        self.assertIn("cv-wp-affiche", x["bodyShown"])
        self.assertEqual(x["rowsDuring"], x["rowsBefore"])                 # aperçu : rangées jamais reconstruites
        self.assertTrue(x["rowsSameNodes"])
        self.assertEqual(x["focusAfterRight"], "Oui, appliquer")
        self.assertEqual(x["focusAfterLeft"], "Non, garder l'ambiance actuelle")
        self.assertEqual(x["sheetPosts"], [])                              # ni événement ni occupation
        self.assertEqual(x["claimsDuringSheet"], 0)                        # aucune action d'assistant réclamée
        self.assertEqual(x["cancelPost"], {"preview_id": x["pid"]})
        self.assertFalse(x["shownAfter"])
        self.assertIn("cv-wp-aurore", x["bodyAfter"])
        self.assertNotIn("cv-preview", x["bodyAfter"])
        self.assertIn("rien n'a changé", x["toast"])
        self.assertFalse(x["reshown"])                                     # jamais réaffichée (même état servi)
        self.assertGreaterEqual(x["claimsAfter"], 1)                       # file reprise après la feuille
        self.assertEqual(x["sheetPanelOverlapQr"], False)
        self.assertEqual(x["sheetInView"], True)

    def test_sheet_pauses_every_ambient_layer(self):
        """Feuille Canvas = écran recouvert : l'aperçu est peint figé — aucune animation continue ne tourne et aucune
        couche n'avance (temps courant ET rendu calculé identiques à 2,5 s d'écart) ; reprise après la fermeture."""
        x = self.r("sheet")
        self.assertEqual(x["motionBefore"]["mode"], "run")
        self.assertGreater(x["motionBefore"]["running"], 0)
        for k in ("motionA", "motionB"):
            m = x[k]
            self.assertEqual(m["mode"], "paused", (k, m))
            self.assertIn("sheet", m["reasons"], (k, m))
            self.assertEqual(m["running"], 0, (k, m))
            self.assertGreater(m["total"], 0, (k, m))                     # aperçu : couches présentes, figées
        self.assertEqual(x["motionA"]["times"], x["motionB"]["times"])
        self.assertEqual(x["motionA"]["styles"], x["motionB"]["styles"])
        self.assertEqual(x["motionAfter"]["mode"], "run")
        self.assertGreater(x["motionAfter"]["running"], 0)
        self.assertEqual(self.r("sheetTop")["motion"]["mode"], "paused")
        self.assertEqual(self.r("sheetTop")["motion"]["running"], 0)

    def test_sheet_footer_names_exact_consent_controls(self):
        x = self.r("sheet")
        no = "↑ ↓ Choisir · OK Non, garder l'ambiance · Retour Non"
        yes = "↑ ↓ Choisir · OK Oui, appliquer · Retour Non"
        self.assertEqual(x["hintNo"], no)
        self.assertEqual(x["hintYes"], yes)
        self.assertEqual(x["hintAfterLeft"], no)
        self.assertEqual((x["focusAfterDown"], x["hintAfterDown"]), ("Oui, appliquer", yes))      # ↓ = bouton du bas
        self.assertEqual((x["focusAfterUp"], x["hintAfterUp"]), ("Non, garder l'ambiance actuelle", no))
        self.assertIn("Parcourir", x["hintAfter"])                        # feuille fermée : aide de l'accueil
        self.assertNotIn("Confirmer", x["hintAfter"])
        self.assertIn("Réglages", self.r("sheetTop")["hintAfter"])

    def test_sheet_choice_is_the_only_visible_focus_and_selection_survives(self):
        x = self.r("sheet")
        p = x["focusProbe"]
        self.assertEqual(p["inside"], ["Non, garder l'ambiance actuelle"])
        self.assertEqual(p["outside"], [])                                # aucune carte/Réglages marqué dessous
        self.assertEqual(p["ringed"], [])                                 # ni anneau blanc ni agrandissement
        self.assertEqual(x["focusProbeYes"]["inside"], ["Oui, appliquer"])
        self.assertEqual((x["focusProbeYes"]["outside"], x["focusProbeYes"]["ringed"]), ([], []))
        self.assertNotEqual(x["selectedBefore"], "Film 1")                # carte déplacée avant la feuille
        self.assertEqual(p["hero"], x["selectedBefore"])                  # le héros décrit toujours la carte choisie
        self.assertEqual(x["selectedAfter"], x["selectedBefore"])         # « Non » : même carte, focus rendu
        t = self.r("sheetTop")
        self.assertEqual(t["before"], "gear")
        self.assertTrue(t["shown"])
        self.assertNotIn("focused", t["gearCls"].split())
        self.assertEqual((t["probe"]["outside"], t["probe"]["ringed"]), ([], []))
        self.assertEqual(t["probe"]["inside"], ["Non, garder l'ambiance actuelle"])
        self.assertFalse(t["shownAfter"])
        self.assertEqual(t["after"], "gear")

    def test_sheets_never_cover_footer_text_and_canvas_sheet_fits_content(self):
        x = self.r("sheet")
        g = x["sheetGeom"]
        self.assertEqual(g["sheets"], ["cvsheet"])
        self.assertEqual(g["footerUnderSheet"], [])
        self.assertLessEqual(g["slack"], 40, g)                           # aucun grand vide sous « Sans réponse… »
        self.assertLessEqual(g["sheetRect"][3], g["helpTop"], g)          # feuille au-dessus du pied de page
        s = self.r("settings")["settingsGeom"]
        self.assertEqual(s["sheets"], ["panel"])
        self.assertEqual(s["footerUnderSheet"], [])
        for name, m in self.r("geometry").items():
            self.assertEqual(m["sheetGeom"]["footerUnderSheet"], [], name)

    def test_sheet_yes_publishes_and_reorders_rows_after_close(self):
        x = self.r("yes")
        self.assertEqual(x["confirmPost"], {"preview_id": x["pid"], "confirm": True})
        self.assertFalse(x["shownAfter"])
        self.assertIn("cv-wp-affiche", x["bodyAfter"])
        self.assertNotIn("cv-preview", x["bodyAfter"])
        self.assertIn("appliquée", x["toast"])
        self.assertEqual(x["rowsAfter"][:2], ["Le choix de ce soir", "Les mieux notés que tu n'as pas vus"])
        self.assertEqual(x["focusIdAfter"], x["focusIdBefore"])            # réancré sur le même film

    def test_sheet_expired_unknown_and_see_state(self):
        x = self.r("errors")
        for k in ("expired", "unknown"):
            self.assertFalse(x[k]["shown"], k)
            self.assertIn("rien n'a changé", x[k]["toast"], k)
        s = x["see_state"]
        self.assertFalse(s["shown"])
        self.assertIn("cv-wp-brume", s["body"])                            # état réel relu par le service
        self.assertNotIn("rien n'a changé", s["toast"])
        self.assertIn("réellement active", s["toast"])
        self.assertFalse(x["lost"]["shown"])
        self.assertNotIn("rien n'a changé", x["lost"]["toast"])

    def test_sheet_timeout_rejects(self):
        x = self.r("timeout")
        self.assertTrue(x["shown"])
        self.assertEqual(x["cancelPost"], {"preview_id": x["pid"]})
        self.assertFalse(x["shownAfter"])
        self.assertIn("Sans réponse", x["toast"])
        self.assertGreaterEqual(x["elapsed_ms"], 900)
        self.assertLess(x["elapsed_ms"], 4000)

    def test_phone_remote_posts_remote_kind_and_drives_sheet(self):
        x = self.r("phone")
        self.assertEqual(x["kinds"], ["remote"])
        self.assertNotEqual(x["focusAfter"], x["focusBefore"])
        self.assertEqual(x["sheetFocus"], "Oui, appliquer")
        self.assertEqual(x["sheetPosts"], [])

    def test_busy_posts_are_serialized_and_latest_wins(self):
        x = self.r("busy")
        self.assertEqual(x["maxInflight"], 1)                              # une requête à la fois
        self.assertEqual(x["serverBusy"], ["panel"])                       # jamais écrasé par l'ancienne liste vide
        self.assertEqual(x["sequence"][-1], ["panel"])
        self.assertEqual(x["hidden"], ["hidden"])
        self.assertEqual(x["offline"], ["offline"])
        self.assertEqual(x["recovered"], [])
        self.assertEqual(x["retried_after_failure"], True)

    def test_present_action_declares_panel_busy_before_done(self):
        x = self.r("present")
        self.assertTrue(x["panel"])
        self.assertLess(x["ui_response_at"], x["done_at"])
        self.assertEqual(x["ui_busy"], ["panel"])
        self.assertEqual(x["max_inflight_ui"], 1)                           # syncBusy : jamais deux envois en vol
        self.assertEqual(x["done_body"]["ok"], True)
        self.assertEqual(x["late_claim_reason"], "tv_busy")               # réclamée puis feuille : jamais appliquée

    def test_no_row_rebuild_under_panel_or_player_then_reanchor(self):
        x = self.r("rows")
        self.assertEqual(x["orderBefore"], x["serverOrder"])               # défaut : ordre du service intact
        self.assertEqual(x["afterPanelOrder"], ["Le choix de ce soir", "Les mieux notés que tu n'as pas vus",
                                                "Parce que tu as aimé « Film 1 »", "Nouveautés disponibles",
                                                "À voir plus tard", "Autres suggestions"])   # « because » reste en place
        self.assertTrue(x["panelSameNodes"])
        self.assertEqual(x["panelOrder"], x["orderBefore"])
        self.assertNotEqual(x["afterPanelOrder"], x["orderBefore"])
        self.assertEqual(x["afterPanelFocus"], x["focusBefore"])
        self.assertTrue(x["playSameNodes"])
        self.assertEqual(x["playOrder"], x["afterPanelOrder"])
        self.assertNotEqual(x["afterPlayOrder"], x["afterPanelOrder"])
        self.assertEqual(x["afterPlayFocusId"], x["playFocusId"])

    def test_pause_static_and_layer_budget(self):
        x = self.r("motion")
        for k in ("panel", "playback", "offline", "hidden", "setup"):
            self.assertEqual(x[k]["running"], 0, (k, x[k]))
            self.assertIn(x[k]["mode"], ("paused", "static"), k)
        self.assertEqual(x["setup"]["body"], "cv-setup")
        self.assertEqual((x["setup"]["mode"], x["setup"]["total"]), ("static", 0))   # configuration : repli fixe
        for k in ("off", "reduced"):
            self.assertEqual((x[k]["mode"], x[k]["total"]), ("static", 0), k)
        self.assertEqual(x["reduced_back"]["mode"], "run")                  # préférence relue en direct
        expect = {"aurore-calm": 3, "aurore-alive": 4, "brume-calm": 3, "brume-alive": 4, "affiche-calm": 1,
                  "affiche-alive": 2}
        for k, n in expect.items():
            self.assertEqual(x["combos"][k]["running"], n, (k, x["combos"][k]))
            self.assertLessEqual(x["combos"][k]["running"], 4)
            for a in x["combos"][k]["anims"]:
                self.assertTrue(set(a["props"]) <= {"transform", "opacity"}, a)
                self.assertIn(a["target"], ("cv-l cv-l1", "cv-l cv-l2", "cv-l cv-l3", "cvglow"), a)
        self.assertEqual(x["resume"]["mode"], "run")
        self.assertGreater(x["resume"]["running"], 0)

    def test_frame_gap_watchdog_degrades_to_static(self):
        x = self.r("watchdog")
        self.assertEqual(x["healthy"]["mode"], "run")
        self.assertEqual(x["healthy"]["degraded"], "0")
        self.assertEqual(x["slow"]["degraded"], "1")
        self.assertEqual(x["slow"]["mode"], "static")
        self.assertEqual(x["slow"]["running"], 0)

    def test_shell_and_qr_are_never_styled_or_animated_by_the_scene(self):
        x = self.r("shell")
        self.assertGreaterEqual(x["variants"], 10)
        self.assertEqual(x["diffs"], [])
        self.assertTrue(x["qrStable"])                                     # pixels du QR identiques dans le temps
        self.assertTrue(x["qrSameAcrossScenes"])

    def test_geometry_populated_sparse_long_message_panels(self):
        g = self.r("geometry")
        for name, m in g.items():
            self.assertEqual(m["ringClip"], [], name)
            self.assertEqual(m["cardQrOverlap"], [], name)
            self.assertFalse(m["panelQrOverlap"], name)
            self.assertFalse(m["footerOverlap"], (name, m))
            self.assertEqual(m["outside"], [], name)
            self.assertTrue(m["qrVisible"], name)
            self.assertTrue(m["heroInMain"], name)
            self.assertLessEqual(m["mainRight"] + 40, m["qrLeft"] + 0.5, name)
            self.assertTrue(os.path.isfile(m["shot"]), name)
            if m.get("bannerTop") is not None:
                self.assertTrue(m["bannerTop"], name)
            if m.get("panelVisible"):
                self.assertEqual(m["panelHiddenBy"], [], name)
        self.assertIn("En attendant", g["sparse"]["text"])
        self.assertEqual(g["sparse"]["focus"], "gear")

    def test_settings_ambiance_owner_flows_and_see_state(self):
        x = self.r("settings")
        self.assertEqual(x["rows"], ["Reconfigurer", "Gérer les assistants", "Ambiance OpenTV", "Fermer"])
        self.assertEqual(x["ambRows"], ["Style", "Mouvement du fond", "Version précédente", "Ambiance d'origine", "Retour"])
        icons = {i["label"]: i for i in x["ambIcons"]}
        for k in ("Version précédente", "Ambiance d'origine"):   # bloc « Flèches » (DejaVu Sans du Pi), jamais ⟲
            self.assertEqual(len(icons[k]["icon"]), 1, icons[k])
            self.assertTrue(0x2190 <= ord(icons[k]["icon"]) <= 0x21FF, icons[k])
            self.assertGreater(icons[k]["glyph"][0], 0, icons[k])
            self.assertLessEqual(icons[k]["glyph"][0], icons[k]["disc"][0], icons[k])
        self.assertEqual(x["stylePost"], {"changes": {"preset": "cinema", "motion": "calm"}, "confirm": True})
        self.assertEqual(x["motionPost"], {"changes": {"motion": "off"}, "confirm": True})
        self.assertEqual(x["undoFocusDefault"], "Non")
        self.assertEqual(x["undoNoPosts"], [])
        self.assertEqual(x["undoPost"], {"confirm": True})
        self.assertEqual(x["resetPost"], {"confirm": True})
        self.assertTrue(x["rowsFrozenInSettings"])
        self.assertIn("cv-wp-affiche", x["bodyAfterStyle"])
        self.assertIn("cv-static", x["bodyAfterMotion"])
        self.assertNotIn("rien n'a changé", x["seeStateToast"])
        self.assertIn("cv-wp-brume", x["seeStateBody"])
        self.assertIn("rien n'a changé", x["noChangeToast"])
        self.assertEqual(x["agentOkInAmbiance"], "settings_forbidden")


# --------------------------------------------------------------------------- navigateur réel : vrai service
@unittest.skipUnless(NODE and playwright_dir() and find_chromium(),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
class TestCanvasRealService(unittest.TestCase):
    """Vrai service OpenTV (API TV, magasin Canvas réel sur disque, horloge monotone réelle) + vraie page /tv : aperçu
    d'un agent par l'App (comme l'Agent Port), feuille, Oui/Non/échéance, occupation de la page, télécommande du
    téléphone, Réglages du propriétaire et vérité après restore_failed. Captures 1920×1080 peuplée et clairsemée."""

    def app(self, catalogue=None):
        kw = {}
        if catalogue is not None:
            d = tempfile.mkdtemp(prefix="otvcat", dir="/tmp")
            self.addCleanup(shutil.rmtree, d, True)
            path = os.path.join(d, "catalogue.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"generated_at": "2026-10-30T00:00:00Z", "items": catalogue}, f)
            kw["OPENTV_CATALOGUE_FILE"] = path
        a = make_app(self, **kw)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.tick()
        # horloge monotone réelle pour l'aperçu (échéance vraie), magasin réel du service
        store = cv.CanvasStore(os.path.join(a.cfg.state_dir, cv.DIR_NAME), mono=time.monotonic, preview_ttl_s=12)
        a.canvas = canvas_tv.TvCanvas(store, a.audit, a.log)
        return a

    def serve(self, a, name):
        s = Servers(self, a)
        shots = shots_dir(self, name)
        ctl, res = os.path.join(shots, "ctl.jsonl"), os.path.join(shots, "ctl-res.jsonl")
        for pth in (ctl, res):
            if os.path.exists(pth):
                os.remove(pth)
        seen = []
        orig_human = a.canvas.human

        def human(kind):
            seen.append(kind)
            return orig_human(kind)
        a.canvas.human = human
        tok, err = a.remote.pair_phone("192.168.2.50", a.remote.pairing()["secret"])   # téléphone appairé
        self.assertIsNone(err)
        phone = a.remote.phones[-1]
        stop = threading.Event()
        fail = {}
        orig_apply = a.canvas.store.owner_apply

        def owner_apply(changes, confirm=False):
            if fail.pop("apply", None):
                raise cv.CanvasError("restore_failed")
            return orig_apply(changes, confirm=confirm)
        a.canvas.store.owner_apply = owner_apply

        def loop():
            done = 0
            while not stop.is_set():
                try:
                    with open(ctl, encoding="utf-8") as f:
                        lines = f.read().splitlines()
                except OSError:
                    lines = []
                for line in lines[done:]:
                    cmd = json.loads(line)
                    out = {"ok": True}
                    try:
                        op = cmd["op"]
                        if op == "preview":
                            out["result"] = a.canvas_agent_preview(cmd.get("agent", "muse-device"), cmd["changes"])
                        elif op == "status":
                            st = a.canvas.store.status()
                            out["result"] = {"busy": st["busy"], "revision": st["revision"], "scene": st["scene"],
                                             "preview": st["preview"] and st["preview"]["preview_id"],
                                             "human": list(seen)}
                        elif op == "phone":
                            out["result"] = a.remote.command(phone, "192.168.2.50", cmd["key"])
                        elif op == "fail_apply":
                            fail["apply"] = True
                        elif op == "audit":
                            with open(a.cfg.audit_file, encoding="utf-8") as f:
                                out["result"] = [json.loads(x)["args"].get("action") for x in f
                                                 if x.strip() and json.loads(x)["tool"] == "canvas"]
                    except cv.CanvasError as e:
                        out = {"ok": False, "error": e.code}
                    with open(res, "a", encoding="utf-8") as f:
                        f.write(json.dumps(out) + "\n")
                    done += 1
                time.sleep(0.02)
        th = threading.Thread(target=loop, daemon=True)
        th.start()
        self.addCleanup(stop.set)
        return {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(), "shots": shots,
                "ctl": ctl, "res": res}

    def test_real_service_lifecycle_settings_and_screenshots(self):
        items = []
        genres = ["Science-fiction, Aventure", "Thriller", "Comédie", "Drame", "Animation, Famille", "Policier"]
        for i in range(24):
            items.append({"detail_path": "b/fakrov/%d" % (300 + i), "site_id": str(300 + i),
                          "title": ("Le Jardin des Possibles, une très longue odyssée familiale" if i == 0 else
                                    "Film %d" % i), "year": 2015 + i % 12, "genre": genres[i % len(genres)],
                          "description": "Résumé du film %d." % i, "imdb_rating": 6.0 + (i % 30) / 10,
                          "imdb_votes": 1000 + i * 37})
        a = self.app(items)
        cfg = self.serve(a, "service")
        out = run_node(self, "e2e_canvas_service.js", cfg)
        st = out["steps"]
        self.assertEqual(st["load"]["busy"], [])                           # page chargée : occupation vide
        self.assertEqual(st["panel"]["busy"], ["panel"])
        self.assertEqual(st["panel_closed"]["busy"], [])
        p = st["sheet"]
        self.assertTrue(p["shown"])
        self.assertEqual(p["kicker"], "Proposé par Muse")
        self.assertIn("cv-preview", p["body"])
        self.assertEqual(p["revision"], 0)                                 # aperçu : rien publié
        self.assertEqual((p["mode"], p["running"]), ("paused", 0))          # feuille : ambiance figée
        self.assertEqual(p["hint"], "↑ ↓ Choisir · OK Non, garder l'ambiance · Retour Non")
        self.assertEqual((p["outside"], p["inside"]), (0, ["Non, garder l'ambiance actuelle"]))
        self.assertEqual(st["no"]["revision"], 0)
        self.assertIsNone(st["no"]["preview"])
        self.assertEqual(st["yes"]["revision"], 1)
        self.assertEqual(st["yes"]["scene"]["wallpaper"], "affiche")
        self.assertIn("cv-wp-affiche", st["yes"]["body"])
        self.assertEqual(st["yes"]["rows"][:2], ["Le choix de ce soir", "Les mieux notés que tu n'as pas vus"])
        self.assertEqual(st["yes"]["focus"], st["yes"]["focusBefore"])
        self.assertIsNone(st["human_cancel"]["preview"])                   # touche humaine : aperçu annulé AVANT
        self.assertFalse(st["human_cancel"]["sheet_seen"])
        self.assertIn("key", st["human_cancel"]["human"])
        self.assertFalse(st["timeout"]["shown"])
        self.assertIsNone(st["timeout"]["preview"])
        self.assertEqual(st["phone"]["human"][-1], "remote")               # télécommande du téléphone
        self.assertNotEqual(st["phone"]["focus"], st["phone"]["focusBefore"])
        self.assertEqual(st["settings_style"]["scene"]["wallpaper"], "brume")
        self.assertEqual(st["settings_motion"]["scene"]["motion"], "off")
        self.assertEqual(st["settings_motion"]["mode"], "static")
        self.assertEqual(st["settings_undo"]["scene"]["motion"], "calm")
        self.assertEqual(st["settings_reset"]["scene"], cv.DEFAULT_SCENE.to_dict())
        self.assertNotIn("rien n'a changé", st["see_state"]["toast"])
        self.assertIn("réellement active", st["see_state"]["toast"])
        acts = st["audit"]
        for want in ("proposed", "rejected", "confirmed", "cancelled", "applied", "rolled-back", "reset"):
            self.assertIn(want, acts)
        for name in ("populated", "sheet", "settings", "cinema"):
            self.assertTrue(os.path.isfile(os.path.join(cfg["shots"], name + ".png")), name)

    def test_real_service_sparse_screenshot(self):
        a = self.app([])
        cfg = self.serve(a, "service-sparse")
        cfg["sparse"] = True
        out = run_node(self, "e2e_canvas_service.js", cfg)
        self.assertIn("En attendant", out["steps"]["sparse"]["text"])
        self.assertEqual(out["steps"]["sparse"]["busy"], [])
        self.assertTrue(os.path.isfile(os.path.join(cfg["shots"], "sparse.png")))


if __name__ == "__main__":
    unittest.main()
