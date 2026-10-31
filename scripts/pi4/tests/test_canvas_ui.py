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

    def test_state_reads_go_through_one_generation_checked_reader(self):
        """Toute lecture de /api/tv/state passe par readState (génération d'adoption capturée à l'envoi) ; toute
        réponse de route Canvas passe par cvRoute (adoption ordonnée, génération incrémentée) ; aucun autre cvAdopt."""
        code = strip_js_comments(read(TV_JS))
        self.assertEqual(code.count('api("GET", "/api/tv/state")'), 1)
        body = re.search(r"function readState\(\) \{(.*?)\n  \}", code, re.S).group(1)
        self.assertIn('api("GET", "/api/tv/state")', body)
        self.assertIn("cvGen", body)
        for m in re.finditer(r"\brender\(([^)]*)\)", code):
            self.assertIn("fresh", m.group(1), m.group(0))                 # jamais un état sans sa fraîcheur
        calls = re.findall(r"(?<!function )\bcvAdopt\(([^)]*)\)", code)
        self.assertEqual(sorted(calls), sorted(["c", "s.canvas"]), calls)   # route (cvRoute) + état frais seulement
        self.assertNotRegex(code, r'capi\("/api/tv/canvas/(confirm|cancel|apply|rollback|reset)')
        m = re.search(r"\bCV_ROUTE_MS = (\d+)[,;]", code)
        self.assertTrue(m and 1000 <= int(m.group(1)) <= 10000, "échéance des routes Canvas bornée")
        self.assertIn("AbortController", code)

    def test_canvas_starts_off_until_first_state(self):
        js = strip_js_comments(read(CANVAS_JS))
        self.assertRegex(js, r"var S = \{ on: false,")

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
        for sel, decl in css_rules(canvas_css()):   # ambiance lente, sauf projection Cinéma explicitement visible
            for d in re.findall(r"animation(?:-duration)?\s*:[^;]*?([\d.]+)s", decl):
                floor = 4 if "cv-wp-affiche" in sel and "cv-m-" in sel else 20
                self.assertGreaterEqual(float(d), floor, (sel, decl))
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


class TestCinemaProjectionStatic(unittest.TestCase):
    """Cinéma (wallpaper « affiche ») : lumière de projection diffuse seulement — aucun projecteur, appareil,
    silhouette, texte ni ressource ; dégradés locaux plumés (aucun filtre/flou/masque) ; couches fixes existantes ;
    « calm » visible à distance, « alive » deux fois plus mobile, tous deux continus sans stroboscope."""
    WALL = {"cv-base", "cv-l", "cv-l1", "cv-l2", "cv-l3", "cvglow"}
    PROPS = {"left", "top", "width", "height", "opacity", "background", "transform", "transform-origin", "display",
             "animation", "animation-duration"}

    def affiche_rules(self):
        out = []
        for sel, decl in css_rules(canvas_css()):
            for part in sel.split(","):
                part = part.strip()
                if "cv-wp-affiche" not in part:
                    continue
                last = re.split(r"\s+", part)[-1]
                out.append((part, last, decl))
        return out

    def keyframes(self, name):
        css = re.sub(r"/\*.*?\*/", "", read(TV_CSS), flags=re.S)
        body = re.search(r"@keyframes\s+%s\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}" % name, css).group(1)
        frames = []
        for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            for o in sel.split(","):
                o = o.strip()
                off = 0.0 if o == "from" else 100.0 if o == "to" else float(o.rstrip("%"))
                frames.append((off, decl))
        return sorted(frames)

    def anim_names(self, motion):
        names = {}
        for sel, decl in css_rules(canvas_css()):
            m = re.search(r"(?:^|;)\s*animation\s*:\s*([\w-]+)\s+([\d.]+)s", decl)
            if not m:
                continue
            for part in sel.split(","):
                part = part.strip()
                if ".cv-anim" in part and "cv-wp-affiche" in part and ("cv-m-" + motion in part or "cv-m-" not in part):
                    names[re.findall(r"[.#]([\w-]+)", re.split(r"\s+", part)[-1])[-1]] = (m.group(1), float(m.group(2)))
        return names

    def test_only_feathered_warm_gradients_on_existing_layers(self):
        rules = self.affiche_rules()
        self.assertTrue(rules)
        lit = set()
        for part, last, decl in rules:
            self.assertNotRegex(part, r"::?(before|after)|::", part)                 # aucun pseudo-élément (forme, texte)
            toks = set(re.findall(r"[.#]([\w-]+)", last))
            if not toks & self.WALL:
                continue                                                               # contenu de l'accueil (hors fond)
            props = set(re.findall(r"([\w-]+)\s*:", decl))
            self.assertLessEqual(props, self.PROPS, (part, props))                    # ni bordure, ombre, rayon, clip…
            low = decl.lower()
            for bad in ("url(", "conic-gradient", "repeating-", "image-set", "element(", "content", "filter", "blur",
                        "mask", "clip", "border", "shadow", "#fff", "white", "hsl"):
                self.assertNotIn(bad, low, (part, bad))
            for g in re.findall(r"(\w[\w-]*)\(", decl):
                self.assertIn(g, ("radial-gradient", "linear-gradient", "rgba", "rotate", "translate3d", "scale",
                                  "ellipse"), (part, g))
            if toks & {"cv-l1", "cv-l2", "cv-l3"} and "background" in props:
                lit.update(toks & {"cv-l1", "cv-l2", "cv-l3"})
                cols = re.findall(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", decl)
                self.assertTrue(cols, part)
                for r, g, b, a in cols:                                                # ivoire chaud, toujours diffus
                    r, g, b, a = int(r), int(g), int(b), float(a)
                    self.assertTrue(r >= 240 and r >= g >= b and r - b >= 30, (part, r, g, b))
                    self.assertLessEqual(a, 0.6, part)
                for grad in re.findall(r"radial-gradient\(([^()]*(?:\([^()]*\)[^()]*)*)\)", decl):
                    alphas = [float(x) for x in re.findall(r"rgba\([^)]*,\s*([\d.]+)\)", grad)]
                    self.assertEqual(alphas[-1], 0, (part, grad))                       # plumé jusqu'à zéro
                    self.assertEqual(alphas, sorted(alphas, reverse=True), (part, grad))
                    self.assertGreaterEqual(len(alphas), 3, (part, grad))               # dégradé en plusieurs paliers
        self.assertEqual(lit, {"cv-l1", "cv-l2", "cv-l3"})
        self.assertRegex(canvas_css(), r"\.cv-wp-affiche \.cv-l1, \.cv-wp-affiche \.cv-l2, \.cv-wp-affiche \.cv-l3 "
                                       r"\{ display: block; \}")

    def test_no_projector_markup_text_or_resource(self):
        html = read(TV_HTML)
        wall = re.search(r'<div id="cvwall">(.*?)</div></div>\n', html, re.S).group(1)
        self.assertEqual(re.findall(r'<div (?:id|class)="([^"]+)"', wall),
                         ["cv-base", "cv-l cv-l1", "cv-l cv-l2", "cv-l cv-l3", "cvglow", "cvga", "cvgb"])
        for f in (TV_HTML, CANVAS_JS, TV_CSS):   # code seulement (les commentaires peuvent nommer l'interdit)
            low = re.sub(r"/\*.*?\*/|<!--.*?-->", "", read(f), flags=re.S).lower()
            for bad in ("projecteur", "projector", "projectionniste", "<img", "<video", "<svg id=\"cv"):
                self.assertFalse(bad in low, (f, bad))

    def test_motion_modes_and_film_cadence_never_strobe(self):
        calm, alive = self.anim_names("calm"), self.anim_names("alive")
        self.assertEqual(set(calm), {"cv-l1", "cv-l2", "cv-l3"}, calm)
        self.assertEqual(set(alive), {"cv-l1", "cv-l2", "cv-l3"}, alive)
        for layer in ("cv-l1", "cv-l2", "cv-l3"):
            self.assertGreaterEqual(calm[layer][1], 8, (layer, calm[layer]))
            self.assertEqual(calm[layer][1], alive[layer][1] * 2, (layer, calm[layer], alive[layer]))
        rot = lambda d: [float(x) for x in re.findall(r"rotate\((-?[\d.]+)deg\)", d)]
        spans = []
        for motion in (calm, alive):
            name = motion["cv-l2"][0]
            r = [x for _, d in self.keyframes(name) for x in rot(d)]
            self.assertEqual(len(r), 2, (name, r))
            spans.append(max(r) - min(r))
        self.assertGreaterEqual(spans[0], 15, spans)                                    # perceptible à distance
        self.assertGreater(spans[1], spans[0], spans)                                  # vivant : angle plus ample
        for motion in (calm, alive):
            film = self.keyframes(motion["cv-l3"][0])
            ops = []
            for o, d in film:
                match = re.search(r"opacity\s*:\s*([\d.]+)", d)
                if match is None:
                    self.fail((o, d))
                ops.append((o, float(match.group(1))))
            self.assertEqual(len(ops), len(film), "respiration : opacité seulement")
            self.assertEqual([o for o, _ in ops], [0, 25, 50, 75, 100])                # aucun creux bref
            for _, d in film:
                self.assertEqual(set(re.findall(r"([\w-]+)\s*:", d)), {"opacity"})
            vals = [v for _, v in ops]
            self.assertGreaterEqual(max(vals) - min(vals), 0.6, vals)                   # visible, mais interpolé


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


@unittest.skipUnless(NODE, "node absent")
class TestCanvasCadenceProbe(unittest.TestCase):
    """Sonde de cadence par rafales bornées (budget CPU Pi) : canvas.js dans node avec rAF et minuteurs simulés et
    pilotables. Jamais de boucle rAF permanente ; rafale immédiate au démarrage/reprise, WIN_MAX intervalles
    consécutifs puis sommeil PROBE_SLEEP_MS sans rAF ; rAF ET minuteur annulés hors mouvement ; rappels périmés
    sans effet ; repli dégradé ≥ BAD_MIN intervalles > GAP_MS parmi WIN_MIN..WIN_MAX d'une même rafale, définitif."""
    RUN = r"""
var vm=require('vm'),fs=require('fs');
function E(id){return {id:id,className:'',hidden:false,textContent:'',attrs:{},setAttribute:function(k,v){this.attrs[k]=v;}};}
function make(){
  var els={};['cvpill','cvga','cvgb'].forEach(function(i){els[i]=E(i);});
  var H={raf:{},rafId:0,cancelled:[],timers:{},tId:0,cleared:[],now:0,listeners:{},hidden:false,mq:{matches:false}};
  var body=E('body');
  var win={requestAnimationFrame:function(cb){H.rafId++;H.raf[H.rafId]=cb;return H.rafId;},
    cancelAnimationFrame:function(id){H.cancelled.push(id);delete H.raf[id];},
    matchMedia:function(){return {get matches(){return H.mq.matches;},addListener:function(f){H.listeners.mq=f;}};}};
  var doc={body:body,get hidden(){return H.hidden;},getElementById:function(i){return els[i]||null;},
    addEventListener:function(k,f){H.listeners[k]=f;}};
  var ctx={window:win,document:doc,setTimeout:function(f,d){H.tId++;H.timers[H.tId]={f:f,d:d};return H.tId;},
    clearTimeout:function(id){H.cleared.push(id);delete H.timers[id];},Object:Object,JSON:JSON,Math:Math,isFinite:isFinite};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
  H.C=ctx.window.OpenTVCanvas;H.body=body;
  H.rafs=function(){return Object.keys(H.raf).length;};
  H.tms=function(){return Object.keys(H.timers).map(function(k){return H.timers[k].d;});};
  H.pump=function(dt){var ids=Object.keys(H.raf);if(!ids.length){return 0;}H.now+=dt;
    ids.forEach(function(id){var cb=H.raf[id];delete H.raf[id];cb(H.now);});return ids.length;};
  H.fire=function(){var ids=Object.keys(H.timers);ids.forEach(function(id){var t=H.timers[id];delete H.timers[id];t.f();});return ids.length;};
  // séquence d'intervalles (ms) : renvoie le nombre de rappels servis avant que la sonde ne s'arrête
  H.run=function(gaps){var n=0;if(H.pump(16)){n++;}for(var i=0;i<gaps.length;i++){if(!H.pump(gaps[i])){break;}n++;}return n;};
  H.snap=function(){var s=H.C.state();return {mode:s.mode,degraded:s.degraded,probing:s.probing,sleeping:s.sleeping,
    probes:s.probes,rafs:H.rafs(),timers:H.tms(),cls:body.className};};
  return H;
}
function rep(v,n){var a=[];for(var i=0;i<n;i++){a.push(v);}return a;}
var SC={schema_version:1,wallpaper:'aurore',motion:'calm',accent:'ice',shade:'balanced',hero_focus:'title',
  row_order:['tonight','watchlist','new','top','more']};
function go(H){H.C.apply(SC,{on:true});H.C.pause([]);}
var out={};
// 1. rafale saine : immédiate, 1 + WIN_MAX rappels, puis sommeil sans rAF ; états répétés : ni relance ni réarmement
var H=make();out.boot=H.snap();go(H);out.start=H.snap();
out.healthyCallbacks=H.run(rep(16,200));out.healthy=H.snap();
var t0=Object.keys(H.timers).join(',');
for(var i=0;i<5;i++){go(H);H.C.apply(SC,{on:true});}
out.repeat=H.snap();out.sameTimer=Object.keys(H.timers).join(',')===t0;
H.fire();out.woke=H.snap();
out.secondCallbacks=H.run(rep(16,200));out.second=H.snap();
// 2. images lentes : dégradé après exactement WIN_MIN intervalles, définitif, plus aucun rAF ni minuteur
H=make();go(H);out.slowCallbacks=H.run(rep(100,200));out.slow=H.snap();
H.C.pause(['panel']);H.C.pause([]);go(H);H.listeners.visibilitychange();out.sticky=H.snap();
// 3. seuils : 44 mauvais sur 90 ⇒ sain ; 45 mauvais (dont 5 bons d'abord) ⇒ dégradé à WIN_MIN, pas avant
H=make();go(H);out.c44=H.run(rep(16,46).concat(rep(100,44)));out.s44=H.snap();
H.fire();out.c45=H.run(rep(16,5).concat(rep(100,45)).concat(rep(16,40)));out.s45=H.snap();
// compteurs remis à zéro à chaque rafale (44 + 44 mauvais en deux rafales ⇒ sain)
H=make();go(H);H.run(rep(16,46).concat(rep(100,44)));H.fire();H.run(rep(100,44).concat(rep(16,46)));out.split=H.snap();
// limite GAP_MS : 70 ms n'est pas un mauvais intervalle, 71 ms l'est
H=make();go(H);H.run(rep(70,90));out.g70=H.snap();
H=make();go(H);out.c71=H.run(rep(71,90));out.g71=H.snap();
// 4. annulation : panneau pendant la rafale ⇒ cancelAnimationFrame, rappel périmé sans relance
H=make();go(H);H.run(rep(16,10));var rid=Object.keys(H.raf)[0],cb=rid?H.raf[rid]:function(){};
H.C.pause(['panel']);out.pPanel=H.snap();out.cancelledRaf=H.cancelled.indexOf(+rid)>=0;
cb(H.now+16);out.pStale=H.snap();
H.C.pause([]);out.pResume=H.snap();
cb(H.now+16);out.pStaleAfter=H.snap();   // rappel périmé pendant la nouvelle rafale : jamais une 2e chaîne
// panneau pendant le sommeil ⇒ clearTimeout, minuteur périmé sans relance, reprise ⇒ rafale immédiate
H.run(rep(16,200));out.pSleep=H.snap();var tid=Object.keys(H.timers)[0],tf=tid?H.timers[tid].f:function(){};
H.C.pause(['hidden']);out.sHidden=H.snap();out.clearedTimer=H.cleared.indexOf(+tid)>=0;
tf();out.sStale=H.snap();
H.C.pause([]);out.sResume=H.snap();
tf();out.sStaleAfter=H.snap();
// page cachée (visibilitychange), configuration, mouvement coupé, réduire les animations : tout est libéré
H.run(rep(16,200));H.hidden=true;H.listeners.visibilitychange();out.vHidden=H.snap();
H.hidden=false;H.listeners.visibilitychange();out.vBack=H.snap();
H.run(rep(16,200));H.C.apply(SC,{on:true,setup:true});out.setup=H.snap();
H.C.apply(SC,{on:true});out.afterSetup=H.snap();
H.run(rep(16,200));var off=JSON.parse(JSON.stringify(SC));off.motion='off';H.C.apply(off,{on:true});out.off=H.snap();
H.C.apply(SC,{on:true});H.run(rep(16,30));H.mq.matches=true;H.listeners.mq();out.reduced=H.snap();
H.mq.matches=false;H.listeners.mq();out.unreduced=H.snap();
H.C.apply(null,{on:false});out.turnedOff=H.snap();
// changement non encore signalé (événement « change »/visibilitychange en retard) : la rafale et le réveil relisent
// le mode en direct et s'arrêtent d'eux-mêmes
H=make();go(H);H.run(rep(16,5));H.mq.matches=true;H.pump(16);out.lateReduced=H.snap();
H=make();go(H);H.run(rep(16,200));H.hidden=true;H.fire();out.lateHidden=H.snap();
process.stdout.write(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        p = subprocess.run([NODE, "-e", cls.RUN, CANVAS_JS], capture_output=True, timeout=60)
        cls.err = p.stderr.decode()
        cls.out = json.loads(p.stdout) if p.returncode == 0 else None

    def setUp(self):
        self.assertIsNotNone(self.out, self.err)

    def idle(self, s, mode):
        self.assertEqual(s["mode"], mode, s)
        self.assertEqual((s["rafs"], s["timers"], s["probing"], s["sleeping"]), (0, [], False, False), s)

    def test_constants_keep_the_documented_budget(self):
        js = strip_js_comments(read(CANVAS_JS))
        m = re.search(r"var GAP_MS = (\d+), WIN_MIN = (\d+), WIN_MAX = (\d+), BAD_MIN = (\d+), "
                      r"PROBE_SLEEP_MS = (\d+);", js)
        self.assertTrue(m, "constantes de la sonde")
        self.assertEqual(tuple(int(x) for x in m.groups()[:4]), (70, 60, 90, 45))
        self.assertTrue(10000 <= int(m.group(5)) <= 15000, m.group(5))
        body = re.search(r"function frame\([^)]*\) \{(.*?)\n  \}", js, re.S).group(1)
        self.assertNotRegex(body, r"\bfor\b|\bwhile\b|\.shift\(|\.push\(", "suivi en temps constant")

    def test_burst_then_sleep_never_a_permanent_loop(self):
        o = self.out
        self.assertEqual((o["boot"]["rafs"], o["boot"]["timers"]), (0, []))        # éteint : aucune sonde
        self.assertEqual((o["start"]["rafs"], o["start"]["probing"], o["start"]["probes"]), (1, True, 1))
        self.assertEqual(o["start"]["timers"], [])
        self.assertEqual(o["healthyCallbacks"], 91)                                 # 1 + WIN_MAX intervalles
        h = o["healthy"]
        self.assertEqual((h["mode"], h["degraded"], h["rafs"], h["probing"], h["sleeping"]), ("run", False, 0, False, True))
        self.assertEqual(len(h["timers"]), 1)
        self.assertTrue(10000 <= h["timers"][0] <= 15000, h["timers"])
        self.assertEqual((o["repeat"]["rafs"], len(o["repeat"]["timers"])), (0, 1))  # états répétés : rien de relancé
        self.assertTrue(o["sameTimer"])
        self.assertEqual((o["woke"]["rafs"], o["woke"]["probing"], o["woke"]["probes"], o["woke"]["timers"]),
                         (1, True, 2, []))
        self.assertEqual(o["secondCallbacks"], 91)
        self.assertEqual((o["second"]["sleeping"], o["second"]["probes"]), (True, 2))

    def test_slow_cadence_degrades_deterministically_and_sticks(self):
        o = self.out
        self.assertEqual(o["slowCallbacks"], 61)                                    # 1 + WIN_MIN intervalles
        self.assertTrue(o["slow"]["degraded"])
        self.idle(o["slow"], "static")
        self.assertIn("cv-static", o["slow"]["cls"])
        self.assertTrue(o["sticky"]["degraded"])
        self.idle(o["sticky"], "static")                                            # définitif pour la session

    def test_degraded_threshold_is_exact_and_per_burst(self):
        o = self.out
        self.assertEqual(o["c44"], 91)
        self.assertFalse(o["s44"]["degraded"])
        self.assertTrue(o["s44"]["sleeping"])
        self.assertEqual(o["c45"], 61)                                              # jamais avant WIN_MIN
        self.assertTrue(o["s45"]["degraded"])
        self.idle(o["s45"], "static")
        self.assertFalse(o["split"]["degraded"])                                    # compteurs par rafale
        self.assertFalse(o["g70"]["degraded"])
        self.assertTrue(o["g71"]["degraded"])
        self.assertEqual(o["c71"], 61)

    def test_pause_cancels_raf_and_timer_and_stale_callbacks_never_restart(self):
        o = self.out
        self.idle(o["pPanel"], "paused")
        self.assertTrue(o["cancelledRaf"])
        self.idle(o["pStale"], "paused")                                            # rappel rAF périmé
        self.assertEqual((o["pResume"]["rafs"], o["pResume"]["probing"], o["pResume"]["timers"]), (1, True, []))
        self.assertEqual(o["pSleep"]["sleeping"], True)
        self.idle(o["sHidden"], "paused")
        self.assertTrue(o["clearedTimer"])
        self.idle(o["sStale"], "paused")                                            # minuteur périmé
        self.assertEqual((o["sResume"]["rafs"], o["sResume"]["probing"], o["sResume"]["timers"]), (1, True, []))
        for k in ("pStaleAfter", "sStaleAfter"):                                    # jamais deux sondes
            base = o[k.replace("StaleAfter", "Resume")]
            self.assertEqual((o[k]["rafs"], o[k]["timers"], o[k]["probes"]), (1, [], base["probes"]), k)

    def test_hidden_setup_off_reduced_release_everything_then_probe_immediately(self):
        o = self.out
        self.idle(o["vHidden"], "paused")
        self.assertEqual((o["vBack"]["rafs"], o["vBack"]["probing"]), (1, True))
        self.idle(o["setup"], "static")
        self.assertEqual((o["afterSetup"]["rafs"], o["afterSetup"]["probing"]), (1, True))
        self.idle(o["off"], "static")
        self.idle(o["reduced"], "static")
        self.assertEqual((o["unreduced"]["rafs"], o["unreduced"]["probing"]), (1, True))
        self.idle(o["turnedOff"], "off")
        for k in ("vHidden", "setup", "off", "reduced", "turnedOff"):
            self.assertFalse(o[k]["degraded"], k)

    def test_probe_rereads_live_mode_before_each_frame_and_wake(self):
        for k in ("lateReduced", "lateHidden"):
            s = self.out[k]
            self.assertEqual((s["rafs"], s["timers"], s["probing"], s["sleeping"], s["degraded"]),
                             (0, [], False, False, False), k)


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
        first = x["firstCanvasPost"]
        self.assertEqual((first["p"], sorted(first["b"])), ("/api/tv/canvas/ui", ["busy", "page", "seq"]))
        self.assertEqual((first["b"]["busy"], first["b"]["seq"]), ([], 1))
        self.assertRegex(first["b"]["page"], r"^[0-9a-f]{32}$")
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

    # ---- occupation datée (page, seq), bail et échéances
    def test_busy_posts_carry_page_and_strictly_increasing_seq(self):
        x = self.r("busySeq")
        first = x["first"]
        self.assertGreaterEqual(len(first), 3)
        for e in first + x["afterStale"] + x["reload"]:
            self.assertEqual(e["keys"], ["busy", "page", "seq"], e)
            self.assertRegex(e["page"], x["hex"])
            self.assertIs(type(e["seq"]), int)
        self.assertEqual({e["page"] for e in first}, {first[0]["page"]})          # un identifiant par chargement
        self.assertEqual([e["seq"] for e in first], list(range(1, len(first) + 1)))   # +1 par envoi
        self.assertEqual([e["busy"] for e in first][:1], [[]])
        st = x["afterStale"]                                                       # « stale » : page remplacée
        self.assertTrue(x["retook"])
        self.assertEqual((st[0]["page"], st[0]["seq"], st[0]["busy"]), (first[0]["page"], len(first) + 1, ["panel"]))
        self.assertNotEqual(st[1]["page"], first[0]["page"])                       # nouvel identifiant…
        self.assertEqual((st[1]["seq"], st[1]["busy"]), (1, ["panel"]))            # …qui reprend la main
        self.assertEqual({e["page"] for e in st[1:]}, {st[1]["page"]})
        self.assertEqual([e["seq"] for e in st[1:]], list(range(1, len(st))))
        rl = x["reload"]                                                           # rechargement : nouvelle page
        self.assertNotIn(rl[0]["page"], {first[0]["page"], st[1]["page"]})
        self.assertEqual((rl[0]["seq"], rl[0]["busy"]), (1, []))
        self.assertIn(st[1]["page"], x["retired"])
        self.assertEqual(x["maxInflight"], 1)

    def test_busy_lease_resends_unchanged_set_and_repairs_service(self):
        x = self.r("busyLease")
        self.assertTrue(x["repaired"], x)
        self.assertLessEqual(x["repairMs"], 10000, x)                              # dans la borne du bail
        leases = x["leases"]
        self.assertGreaterEqual(len(leases), 3, leases)                            # ~29 s sans aucun changement
        for e in leases:
            self.assertEqual(e["busy"], ["panel"], e)
        seqs = [e["seq"] for e in leases]
        self.assertEqual(seqs, sorted(set(seqs)))
        ts = [0] + [e["t"] for e in leases]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        self.assertLessEqual(max(gaps), 10000, gaps)                               # au plus toutes les 10 s
        self.assertGreaterEqual(min(gaps[1:]), 4000, gaps)                         # jamais une rafale
        self.assertEqual(x["maxInflight"], 1)
        self.assertTrue(x["panelOpen"])

    def test_busy_timeout_forgets_ack_and_resends_current_set(self):
        x = self.r("busyLost")
        self.assertTrue(x["panelOpen"])
        self.assertEqual(x["hung"]["busy"], [])
        self.assertTrue(x["resent"], x)
        r = x["resend"]
        self.assertEqual((r["busy"], r["seq"]), (["panel"], x["hung"]["seq"] + 1))
        self.assertGreaterEqual(r["ms"], 4500, r)                                  # après l'échéance CV_ROUTE_MS…
        self.assertLess(r["ms"], 6500, r)                                          # …tout de suite, jamais au bail
        self.assertEqual(x["serverBusy"], ["panel"])

    def test_offline_overlay_consumes_every_ordinary_key(self):
        x = self.r("offlineKeys")
        self.assertTrue(x["panelBefore"]["shown"])
        self.assertTrue(x["offline"])
        self.assertTrue(x["stillOffline"])
        self.assertEqual(x["posts"], [])                  # ni Regarder, ni avis, ni geste, ni réclamation
        self.assertIsNotNone(x["focusBefore"])
        self.assertEqual((x["focusDuring"], x["cardDuring"]), (x["focusBefore"], x["cardBefore"]))
        self.assertEqual(x["panelDuring"], x["panelBefore"])   # fiche intacte sous le voile
        self.assertTrue(x["recovered"])                   # reprise automatique non gênée
        self.assertNotEqual(x["focusAfter"], x["focusDuring"])   # touches de nouveau actives

    def test_owner_write_timeout_blocks_duplicates_until_state_reread(self):
        x = self.r("ownerLost")
        self.assertEqual(x["focusYes"], "btn:Oui, revenir")
        self.assertTrue(x["lostToast"])
        self.assertGreaterEqual(x["lostMs"], 4500)
        self.assertEqual(x["where"], "Ambiance OpenTV")
        self.assertEqual(x["postsLocked"], 1)             # issue inconnue : jamais un second « Version précédente »
        self.assertIn("Vérification", x["lockedToast"])
        self.assertEqual(x["postsAfter"], 2)              # état réel relu : de nouveau permis

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
        for k in ("panel", "playback", "offline", "hidden", "setup", "off", "reduced"):
            self.assertEqual(x[k]["running"], 0, (k, x[k]))
            self.assertIn(x[k]["mode"], ("paused", "static"), k)
            self.assertEqual((x[k]["probe"]["probing"], x[k]["probe"]["sleeping"]), (False, False), k)   # rien en attente
        self.assertEqual(x["setup"]["body"], "cv-setup")
        self.assertEqual((x["setup"]["mode"], x["setup"]["total"]), ("static", 0))   # configuration : repli fixe
        for k in ("off", "reduced"):
            self.assertEqual((x[k]["mode"], x[k]["total"]), ("static", 0), k)
        self.assertEqual(x["reduced_back"]["mode"], "run")                  # préférence relue en direct
        # Cinéma/affiche Calme garde trois couches animées bornées : balayage, faisceau et respiration diffuse.
        expect = {"aurore-calm": 3, "aurore-alive": 4, "brume-calm": 3, "brume-alive": 4, "affiche-calm": 3,
                  "affiche-alive": 4}
        for k, n in expect.items():
            self.assertEqual(x["combos"][k]["running"], n, (k, x["combos"][k]))
            self.assertLessEqual(x["combos"][k]["running"], 4)
            for a in x["combos"][k]["anims"]:
                self.assertTrue(set(a["props"]) <= {"transform", "opacity"}, a)
                self.assertIn(a["target"], ("cv-l cv-l1", "cv-l cv-l2", "cv-l cv-l3", "cvglow"), a)
        self.assertEqual(x["resume"]["mode"], "run")
        self.assertTrue(x["resume"]["probe"]["probing"] or x["resume"]["probe"]["sleeping"], x["resume"])
        self.assertGreater(x["resume"]["running"], 0)

    def test_frame_gap_watchdog_degrades_to_static(self):
        x = self.r("watchdog")
        self.assertEqual(x["healthy"]["mode"], "run")
        self.assertEqual(x["healthy"]["degraded"], "0")
        self.assertEqual(x["healthy"]["probe"], {"probing": False, "sleeping": True, "probes": 1})   # rafale finie
        self.assertEqual(x["asleep"]["probe"], {"probing": False, "sleeping": True, "probes": 1})    # sommeil ≥ 9 s
        self.assertEqual(x["asleep"]["running"], x["healthy"]["running"])          # couches CSS toujours animées
        self.assertGreater(x["healthy"]["running"], 0)
        self.assertEqual(x["slow"]["degraded"], "1")
        self.assertEqual(x["slow"]["mode"], "static")
        self.assertEqual(x["slow"]["running"], 0)
        self.assertEqual(x["slow"]["probe"]["probes"], 2)
        # fin du sommeil (≤ 15 s après la rafale saine) puis 60 intervalles lents : jamais avant, jamais bien après
        self.assertTrue(60 <= x["slowSteps"] <= 60 + 70, x["slowSteps"])
        self.assertEqual(x["after"]["probe"], {"probing": False, "sleeping": False, "probes": 2})
        self.assertEqual((x["after"]["mode"], x["after"]["degraded"]), ("static", "1"))

    def test_cinema_projection_light_is_diffuse_warm_and_behind(self):
        """Captures réelles 1920×1080 (projection-off|calm|alive[-light].png) : lumière ivoire chaude, large et
        plumée, point chaud hors champ (aucun appareil), jamais au-dessus du héros/des cartes/de la coque."""
        x = self.r("projection")
        for mo, mode, n in (("off", "static", 0), ("calm", "run", 3), ("alive", "run", 4)):
            r = x[mo]
            self.assertEqual((r["mode"], r["running"]), (mode, n), mo)
            self.assertEqual(r["behind"], [], mo)                                       # toujours derrière
            for layer in r["layers"]:                                                   # aucune forme ni texte ajoutés
                self.assertIn(layer["before"], ("none", "normal"), (mo, layer))
                self.assertIn(layer["after"], ("none", "normal"), (mo, layer))
                self.assertEqual(layer["text"].strip(), "", (mo, layer))
                self.assertEqual(layer["kids"], 2 if layer["cls"] == "cvglow" else 0, (mo, layer))
            px = r["px"]
            self.assertLessEqual(px["rawMax"], 180, (mo, px))                           # aucune source blanche éclatante
            self.assertLessEqual(px["maxStep"], 9, (mo, px))                            # aucun bord ni silhouette
            self.assertGreaterEqual(px["maxDelta"], 50, (mo, px))                       # visible à distance
            self.assertGreaterEqual(px["meanDelta"], 15, (mo, px))
            self.assertGreaterEqual(px["litFrac"], 0.5, (mo, px))                       # large et diffuse
            self.assertGreaterEqual(px["warmth"], 4, (mo, px))                          # ivoire chaud
            ax, ay = px["maxAt"]
            self.assertTrue(ax >= 960 and ay <= 600, (mo, px["maxAt"]))                  # projection ancrée en haut à droite
        self.assertEqual(x["off"]["moved"], 0)                                          # « Coupé » : fixe
        self.assertGreater(x["calm"]["moved"], 0.2)
        self.assertGreater(x["alive"]["moved"], x["calm"]["moved"])                     # « Vivant » plus présent
        c, a = x["calm"]["cadence"], x["alive"]["cadence"]
        for cadence in (c, a):
            self.assertEqual(cadence["samples"], round(cadence["duration"] * 24 / 1000) + 1, cadence)
            self.assertLessEqual(cadence["maxStep"], 0.03, cadence)                      # variation continue à 24 i/s
            self.assertGreaterEqual(cadence["max"] - cadence["min"], 0.6, cadence)
        self.assertLess(a["duration"], c["duration"])                                  # vivant deux fois plus mobile
        self.assertEqual((x["reduced"]["mode"], x["reduced"]["total"]), ("static", 0))
        self.assertEqual(x["reducedVsOff"], 0)                                          # repli fixe = « Coupé »

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

    # ---- correctif tâche 3 : réponses de route qui font foi, consentement jamais invisible
    def assert_no_stale_adoption(self, x, name):
        self.assertTrue(x["laterPollAdopted"], (name, x))                  # poll légitime suivant : adopté
        self.assertEqual(x["wallpapers"], [x["want"]] if x["wallpapers"] else [], (name, x))   # aucun ancien fond
        self.assertEqual(x["wpNow"], x["want"], (name, x))
        self.assertTrue(x["rowsSameNodes"], (name, x))                     # aucune reconstruction des rangées
        self.assertEqual(x["rowsAfter"], x["rowsBefore"], (name, x))
        self.assertFalse(x["sheetShown"], (name, x))

    def test_stale_state_poll_never_overrides_confirm_response(self):
        x = self.r("staleConfirm")
        self.assertEqual(x["confirmPosts"], 1)
        self.assertIn("Version précédente", x["ambRows"])                  # can_rollback de la réponse, jamais l'ancien
        self.assertEqual(x["rowsBefore"][:2], ["Le choix de ce soir", "Les mieux notés que tu n'as pas vus"])
        self.assert_no_stale_adoption(x, "confirm")

    def test_stale_state_poll_never_overrides_owner_apply_rollback_reset(self):
        x = self.r("staleOwner")
        for name in ("apply", "rollback", "reset"):   # « reset » : révision qui recule (0), garde par génération
            self.assertEqual(x[name]["posts"], 1, name)
            self.assert_no_stale_adoption(x[name], name)

    def test_late_older_route_response_never_overrides_newer_route(self):
        x = self.r("staleRoute")
        self.assertTrue(x["applied"])
        self.assertLess(x["withinRouteDeadline"], 5000)                    # réponse tardive livrée avant l'échéance
        self.assertEqual(x["wallpapers"], [], x)                            # « Non » tardif : jamais l'ancien fond
        self.assertEqual(x["wpNow"], "cv-wp-affiche")

    def test_older_state_read_never_overrides_newer_state_read(self):
        x = self.r("staleGet")
        self.assertIn("Réponse perdue", x["toast"])
        self.assertEqual(x["rowsBefore"][:2], ["Le choix de ce soir", "Les mieux notés que tu n'as pas vus"])
        self.assert_no_stale_adoption(x, "get-order")

    def test_offline_overlay_rejects_open_sheet_and_blocks_its_keys(self):
        x = self.r("offlineSheet")
        self.assertTrue(x["shown"])
        self.assertTrue(x["offline"])
        self.assertFalse(x["sheetAtOverlay"])                              # jamais de feuille sous le voile
        shows = [v for v in x["vis"] if v["id"] == "offline" and v["shown"]]
        hides = [v for v in x["vis"] if v["id"] == "cvsheet" and not v["shown"]]
        self.assertTrue(shows and hides, x["vis"])
        self.assertLessEqual(hides[0]["t"] - shows[0]["t"], 50, x["vis"])   # refus dans la même tâche que le voile
        self.assertEqual(x["cancelPosts"], [{"preview_id": x["pid"]}])     # annulation tentée (ici pendue)
        self.assertEqual(x["confirmPosts"], 0)                             # ↓ puis OK : jamais de « Oui »
        self.assertEqual(x["published"], {"wp": "aurore", "rev": 0})
        self.assertTrue(x["recovered"])
        self.assertFalse(x["reshown"])
        self.assertEqual(x["wpAfter"], "cv-wp-aurore")
        f = x["forced"]                                                    # voile visible, feuille ouverte
        self.assertTrue(f["shown"])
        self.assertEqual(f["focus"], "Non, garder l'ambiance actuelle")     # ↓ sans effet
        self.assertEqual(f["posts"], [])                                   # OK sans effet : ni Oui ni Non envoyés

    def test_hung_yes_is_bounded_and_rereads_actual_state(self):
        x = self.r("hungYes")
        self.assertTrue(x["shownDuring"])
        self.assertEqual(x["busyCancel"], 0)
        self.assertTrue(x["closed"], x)
        self.assertGreaterEqual(x["elapsed_ms"], 3500, x)
        self.assertLess(x["elapsed_ms"], 9000, x)
        self.assertIn("Réponse perdue", x["toast"])
        self.assertNotIn("rien n'a changé", x["toast"])
        self.assertTrue(x["refetched"])
        self.assertTrue(x["keysFree"])

    def test_boot_keeps_v23_until_first_state_and_never_flashes_canvas_before_setup(self):
        x = self.r("boot")
        for name in ("setup", "home"):
            self.assertEqual(x[name]["pre"]["body"], "", (name, x[name]))
            self.assertEqual(x[name]["pre"]["mode"], "off", (name, x[name]))
            self.assertEqual(x[name]["pre"]["wall"], "none", (name, x[name]))
        self.assertEqual(x["setup"]["final"], "cv-setup")
        self.assertEqual([c for c in x["setup"]["hist"] if "cv-wp-" in c], [], x["setup"]["hist"])
        self.assertIn("cv-wp-aurore", x["home"]["final"])

    def test_unavailable_store_offers_no_failing_write(self):
        x = self.r("unavailable")
        self.assertEqual(x["rows"], ["Retour"])
        self.assertIn("indisponible", x["text"].lower())
        self.assertNotIn("Ambiance d'origine", x["text"])
        self.assertNotIn("répare", x["text"])
        self.assertEqual(x["back"], "Réglages")
        self.assertIn("Indisponible", x["sub"])

    def test_reduced_motion_and_static_drop_halo_crossfade(self):
        x = self.r("motion")
        self.assertEqual(x["haloRun"], ["opacity/0.6s"] * 2)
        self.assertEqual(x["haloOff"], ["none/0s"] * 2)
        self.assertEqual(x["haloReduced"], ["none/0s"] * 2)

    def test_scene_pill_fits_dateline(self):
        g = self.r("load")["pillGeom"]
        self.assertGreaterEqual(g["pill"][0], g["line"][0] - 0.5, g)
        self.assertLessEqual(g["pill"][1], g["line"][1] + 0.5, g)
        self.assertLessEqual(g["pill"][2], g["line"][2], g)


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
        # panne réelle : verrou du magasin tenu > CV_ROUTE_MS ; la liste vide de la page est livrée APRÈS un
        # ensemble plus récent de la même page (désordre des connexions), puis appliquée par le vrai TvCanvas
        late, newer = {"armed": False, "result": None}, threading.Event()
        orig_page_busy = a.canvas.page_busy

        def page_busy(reasons, page, seq):
            if late["armed"] and reasons == []:
                late["armed"] = False
                newer.wait(20)
                time.sleep(0.3)
                done = orig_page_busy(reasons, page, seq)
                late["result"] = {"applied": done, "busy": list(a.canvas.store.busy_reasons())}
                return done
            done = orig_page_busy(reasons, page, seq)
            if reasons and not late["armed"] and late["result"] is None:
                newer.set()
            return done
        a.canvas.page_busy = page_busy

        def stall(hold_s):
            held = threading.Event()

            def hold():
                with a.canvas.store._mu:
                    held.set()
                    time.sleep(hold_s)
            newer.clear()
            late["armed"] = True
            threading.Thread(target=hold, daemon=True).start()
            return held.wait(2)

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
                        elif op == "stall":
                            out["result"] = stall(cmd["hold_s"])
                        elif op == "late":
                            out["result"] = dict(late)
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

    def test_real_service_stalled_store_never_leaves_page_busy_stale(self):
        """Verrou du magasin tenu 6,5 s (> échéance de la page) pendant que le propriétaire ferme puis rouvre
        Réglages : la vieille liste vide arrive en retard ; dans la borne du bail, le vrai service connaît « panel »
        (liste tardive périmée, sans effet) et refuse tout aperçu d'agent (`ui_busy`)."""
        a = self.app([{"detail_path": "b/fakrov/%d" % (300 + i), "site_id": str(300 + i), "title": "Film %d" % i,
                       "year": 2020, "genre": "Drame", "description": "Résumé.", "imdb_rating": 7.0,
                       "imdb_votes": 1000 + i} for i in range(8)])
        cfg = self.serve(a, "service-stall")
        cfg["stall"] = 6.5
        x = run_node(self, "e2e_canvas_service.js", cfg)["steps"]["stall"]
        self.assertEqual(x["before"], ["panel"])
        self.assertTrue(x["held"])
        self.assertTrue(x["panelOpen"])
        self.assertTrue(x["repaired"], x)
        self.assertLess(x["elapsedMs"], 6500 + 10000, x)              # verrou + borne du bail
        self.assertIn("panel", x["busy"])
        self.assertEqual(x["late"]["applied"], False, x)               # liste vide tardive : périmée…
        self.assertIn("panel", x["late"]["busy"])                      # …« panel » jamais retiré
        self.assertEqual(x["preview"], {"ok": False, "error": "ui_busy"})
        self.assertEqual(x["closed"], [])                              # fermeture réelle : de nouveau libre
        self.assertTrue(x["previewAfter"]["ok"], x)

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
