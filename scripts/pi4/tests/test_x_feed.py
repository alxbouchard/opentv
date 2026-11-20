"""X TV 0.7.14 — fil X à la TV : largeur réelle 1080p, médias et vidéos de X intacts, J'aime / Enregistrer (signet)
du post choisi à la télécommande, par le VRAI geste de Chromium.

- statique : x.css ne masque jamais un [role=group] hors de la rangée d'actions (lecteur, commandes vidéo, galeries) ni
  ne restyle un média ; x.js n'appelle jamais click(), dispatchEvent, submit, play() ni une API ; seuls focus() et
  blur() touchent aux boutons ; aucune mémoire d'état du compte ; vidéos de test exactes (sha256) ;
- bout en bout (Chromium complet réel, extension installée par install.sh, fixtures LOCALES anonymes, aucun compte) :
  témoin AVANT (extension 0.7.13 du commit de base) puis APRÈS : géométrie de la colonne, du post et des lignes de
  texte, médias sans déformation, vidéos H.264 (MSE et progressive) et VP9 à l'écran (pixel de la capture + image
  décodée), commandes vidéo visibles, aucune lecture lancée ; → / ← / ↑ ↓ / OK, répétitions, DOM réutilisé, posts
  cités, bouton absent, post long, ouverture, retour, partage, fenêtre modale et connexion ; mutants des gardes.
Limite : la fixture imite la chaîne react-native-web de X ; le DOM réel de x.com n'a pas été mesuré (refus du
navigateur sans tête) et la session X réelle sur la Pi (Wayland/HDMI, CEC) n'est PAS couverte ici.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests._common import PI4
from tests.test_x_tv import EXT, EXT_DST, INSTALL, NODE, code_only, full_chrome, read, sh
from opentv_pi import VERSION, xcontext

XJS = os.path.join(EXT, "x.js")
XCSS = os.path.join(EXT, "x.css")
E2E = os.path.join(PI4, "tests", "e2e_x_feed.js")
VIDEO_DIR = os.path.join(PI4, "tests", "fixtures", "x-video")
# vidéos synthétiques anonymes (640×360, 3 s, première image rouge ; métadonnées : encodeur seulement)
VIDEO_SHA = {"h264_prog.mp4": "4ded146a108249fea98cc40b108b1015cccd17948845601c9aed75938e1886c2",
             "h264_frag.mp4": "4316ff13c03bb6d5099a1eed2d8e1e72f6385047494aee3d60442d902313feb0",
             "vp9.webm": "afeacd6b84980456989fff9b762e24c341a4dc0befc1d99e69cb172f5771da42"}
BASELINE = "746cd87"   # 0.7.13 déployée : témoin « avant »
REPO = os.path.dirname(os.path.dirname(PI4))
ACTION_LINE = 'const ACTION_TESTIDS = ["like", "unlike", "bookmark", "removeBookmark"];'
# 0.7.15 : les SEULS masquages du fil dans un post : contrôles identifiés un par un, jamais un conteneur
COL = 'html.otvx-tv [data-testid="primaryColumn"] '
HIDDEN_CONTROLS = (
    COL + 'article :is(button, [role="button"]):is([data-testid="reply"], [data-testid="retweet"], '
    '[data-testid="unretweet"])',
    COL + 'article a[href*="/status/"][href$="/analytics"]',
    COL + 'article article :is(button, [role="button"]):is([data-testid="like"], [data-testid="unlike"], '
    '[data-testid="bookmark"], [data-testid="removeBookmark"])',
)
# revue de 2f4c74d : TOUS les autres masquages (display:none), indépendamment du mot « article » — interface de la PAGE
# seulement (jamais un ancêtre du fil), chacun exclu de l'intérieur d'un post par :not(article *) ; plus le message
# de l'extension caché. AUCUNE enveloppe générique (placementTracking retiré, sans heuristique de remplacement).
PAGE_CHROME = tuple("html.otvx-tv " + s + ":not(article *)" for s in (
    'header[role="banner"]', '[data-testid="sidebarColumn"]', '[data-testid="DMDrawer"]',
    '[data-testid="GrokDrawer"]', '[data-testid="BottomBar"]', '[data-testid="sheetDialog"] ~ [data-testid="mask"]',
    '[data-testid="primaryColumn"] [data-testid="toolBar"]',
    '[data-testid="primaryColumn"] [data-testid="tweetTextarea_0_label"]'))
OWN_UI_HIDDEN = ("html.otvx-tv .otvx-toast[hidden]",)
ALL_HIDDEN = PAGE_CHROME + HIDDEN_CONTROLS + OWN_UI_HIDDEN
# enveloppes et ancêtres possibles d'un média ou du fil : jamais nommés par un masquage, nulle part
NEVER_HIDDEN_TOKENS = ("placementTracking", "cellInnerDiv", "tweetPhoto", "videoPlayer", "videoComponent", "card.",
                       "role=\"group\"", "role=\"link\"", "role=\"region\"", ":has(", "main", "section", "div", "span",
                       "[aria-label", "[class", "[style", "data-otvx")
STALE = "Le fil a changé : choisis de nouveau le post avec ↑ ↓, puis →."
NOACT = "Ce post n'a pas de bouton J'aime ou Enregistrer visible."
NOKEY = "Reviens une fois à l'accueil OpenTV (touche Accueil), puis réessaie."


def no_comments(js):
    return code_only(re.sub(r"/\*.*?\*/", "", js, flags=re.S))


def split_top(sel):
    """Sélecteurs d'une règle, séparés aux virgules de PREMIER niveau (jamais dans :is/:has/:not)."""
    out, depth, cur = [], 0, ""
    for ch in sel:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [s for s in out if s]


def css_rules(css):
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(split_top(m.group(1)), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body)]


class TestFeedStatic(unittest.TestCase):
    def test_video_fixtures_exact(self):
        self.assertEqual(sorted(os.listdir(VIDEO_DIR)), sorted(VIDEO_SHA))
        for name, sha in VIDEO_SHA.items():
            with open(os.path.join(VIDEO_DIR, name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), sha, name)

    def test_css_never_hides_media_or_generic_groups(self):
        """0.7.15 : dans un post, seuls des contrôles identifiés PAR EUX-MÊMES sont masqués (Répondre, Republier,
        statistiques ; boutons d'un post cité) : aucun [role=group], aucune enveloppe ni élément sans marqueur visé,
        aucun data-otvx-bar. Aucun média restylé (ni object-fit ni hauteur). Revue de 2f4c74d : la liste exacte couvre
        TOUS les display:none (avec ou sans « article ») ; aucune enveloppe générique (placementTracking…)."""
        rules = css_rules(read(XCSS))
        self.assertTrue(rules)
        hides = []
        for sels, body in rules:
            for s in sels:
                self.assertTrue(s.startswith("html.otvx-tv"), s)   # inerte hors du fil
                self.assertNotIn("data-otvx-bar", s)
                self.assertNotIn("placementTracking", s)
                if re.search(r"display\s*:\s*none", body):
                    hides.append(s)
                    self.assertNotIn('role="group"', s)
                    self.assertNotIn(":has(", s)
                    for tok in NEVER_HIDDEN_TOKENS:
                        self.assertNotIn(tok, s.replace('[data-testid="primaryColumn"] ', ""), (tok, s))
                elif 'role="group"' in s:   # autre règle : seulement les vrais boutons d'action (icônes, taille)
                    self.assertRegex(s, r'\[role="group"\] (:is\()?\[data-testid="(like|unlike|bookmark|removeBookmark)"\]')
                for media in ("video", "videoPlayer", "videoComponent", "tweetPhoto", "img", "card"):
                    self.assertNotRegex(s, r"\b%s\b" % media, s)   # aucun média ciblé, nulle part
                for kw in ("visibility", "opacity: 0", "height: 0", "max-height", "clip", "content-visibility"):
                    self.assertNotIn(kw, body.replace("visibility: visible", ""), (kw, s))   # aucun autre masquage
        self.assertEqual(tuple(hides), ALL_HIDDEN)                   # liste EXACTE de tous les masquages
        for s in PAGE_CHROME:                                         # interface de page : jamais dans un post
            self.assertTrue(s.endswith(":not(article *)"), s)
            self.assertEqual(s.count("article"), 1, s)
        css = read(XCSS)
        for bad in ("object-fit", "max-height", "autoplay", "@import", "url("):
            self.assertNotIn(bad, re.sub(r"/\*.*?\*/", "", css, flags=re.S), bad)
        for f in ("x.js", "nav.js", "xentry.js", "background.js"):
            src = no_comments(read(os.path.join(EXT, f)))
            for bad in (".play(", "autoplay", ".pause(", "currentTime", ".muted"):
                self.assertNotIn(bad, src, (f, bad))                   # aucune lecture forcée ni arrêtée

    def test_xjs_actions_are_focus_only_and_native(self):
        """x.js : identifiants des VRAIS boutons sur UNE ligne exacte ; seuls focus() et blur() les touchent ; OK sur un
        bouton : rien ici (Chromium active), sinon bloqué (répétition, bouton qui n'est plus celui du post choisi)."""
        raw = read(XJS)
        src = no_comments(raw)
        self.assertEqual(src.count(ACTION_LINE), 1)
        rest = src.replace(ACTION_LINE, "")
        for bad in (".click(", "dispatchEvent", "requestSubmit", ".submit(", "KeyboardEvent", "MouseEvent",
                    "PointerEvent", "new Event(", "fetch(", "XMLHttpRequest", "WebSocket", "localStorage",
                    "sessionStorage", "chrome.storage", "indexedDB", "document.cookie", "innerHTML", "eval(",
                    "retweet", "reply", "tweetButton", "\"like\"", "'like'", "unlike", "bookmark"):
            self.assertNotIn(bad, rest, bad)
        self.assertEqual(src.count("sendMessage("), 1)                    # partage seulement (←)
        self.assertEqual(src.count(".focus("), 1)                        # focusAction : focus natif seulement
        self.assertIn("btn.focus({ preventScroll: true });", src)
        ok = src[src.index('if (a === "ok" && acting) {'):src.index('const feed = feedMode();')]
        self.assertLess(src.index(ok), src.index('if (!feed || dialogOpen()'))
        self.assertIn("if (e.repeat || !actionValid(ae) || dialogOpen()) {", ok)
        calls = set(re.findall(r"([A-Za-z_.]+)\(", ok))
        self.assertEqual(calls, {"actionValid", "leaveButton", "toast", "dialogOpen",
                                 "e.preventDefault", "e.stopImmediatePropagation", "NAV.markHandled"}, calls)
        av = src[src.index("function actionValid(node)"):src.index("function permalink(post)")]
        for need in ("node === actEl", "actEl.isConnected", "ownerPost(actEl) === focused", "actKey !== null",
                     "permalink(focused) === actKey", "actions(focused).indexOf(actEl) >= 0"):
            self.assertIn(need, av)
        lf = src[src.index('} else if (a === "left") {'):src.index("} else {   //")]
        self.assertIn("leftGuardUntil = performance.now() + LEFT_GUARD_MS;", lf)
        self.assertIn("else if (!e.repeat && performance.now() >= leftGuardUntil) { shareFocused(); }", lf)
        self.assertEqual(int(re.search(r"const LEFT_GUARD_MS = (\d+);", src).group(1)), 700)
        # rangée d'actions : jamais un groupe qui contient un média ; boutons du post lui-même seulement
        self.assertIn("return rowOf(post);", src)   # 0.7.15 : rangée la plus interne de CE post, même mixte
        self.assertIn("ownerPost(b) === post && usable(b)", src)
        self.assertIn("if (!e.isTrusted) { return; }", src)

    def test_help_and_docs_french(self):
        src = read(XJS)
        for need in ("J'aime / Enregistrer", "Partager à Hermit ou Muse", "Bouton suivant", "Revenir au post",
                     "Aimé · OK retire", "Enregistré · OK retire"):
            self.assertTrue(need in src or need in read(XCSS), need)
        readme = read(os.path.join(PI4, "README.md"))
        for need in ("0.7.14", "0.7.15", "J'aime", "Enregistrer", "→", "Aimé · OK retire"):
            self.assertIn(need, readme)
        self.assertEqual(VERSION, "0.7.22")
        self.assertEqual(json.loads(read(os.path.join(EXT, "manifest.json")))["version"], VERSION)


@unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT") and full_chrome(),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT + OPENTV_TEST_CHROME_FULL (Chromium complet) + node")
class TestFeedEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="otvxfeed", dir="/tmp")
        p = sh([INSTALL, "--root", cls.root, "--skip-preflight"])
        assert p.returncode == 0, p.stderr.decode()[-2000:]
        cls.ext = os.path.join(cls.root, EXT_DST.lstrip("/"))           # extension telle qu'installée
        cls.shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvxfeedshots", dir="/tmp")
        os.makedirs(cls.shots, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def run_e2e(self, ext, mode, label, shots=True):
        cfg = {"chrome": full_chrome(), "ext": ext, "ext_id": xcontext.EXTENSION_ID, "mode": mode, "label": label,
               "shots": self.shots if shots else "", "video_dir": VIDEO_DIR}
        p = subprocess.run([NODE, E2E, os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)], capture_output=True,
                           timeout=240)   # un seul Chromium à la fois
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-3000:] + p.stderr.decode()[-3000:])
        return json.loads(p.stdout.decode().strip().splitlines()[-1])

    def assert_red(self, px, what):
        self.assertTrue(px and px[0] >= 200 and px[1] <= 90 and px[2] <= 60, (what, px))

    def test_before_baseline_reproduces_field_photo(self):
        """Témoin : l'extension 0.7.13 sur la MÊME fixture reproduit la photo terrain (post ≈ 1/3 de l'écran, bouts de
        lignes, aucune action) et masque la vidéo enveloppée d'un [role=group] et toutes les commandes vidéo."""
        if not shutil.which("git") or subprocess.run(["git", "-C", REPO, "cat-file", "-e", BASELINE + "^{commit}"],
                                                     capture_output=True).returncode:
            self.skipTest("historique git %s absent" % BASELINE)
        d = tempfile.mkdtemp(prefix="otvxold", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        arc = subprocess.run(["git", "-C", REPO, "archive", BASELINE, "scripts/pi4/x-tv-extension"],
                             capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=arc, check=True)
        r = self.run_e2e(os.path.join(d, "scripts", "pi4", "x-tv-extension"), "geom", "before")
        g = r["geom"]
        self.assertLessEqual(g["p1"][2], 640, g["p1"])                    # ≈ 1/3 de 1920
        self.assertLessEqual(g["lines"]["chars_per_line"], 40, g["lines"])
        self.assertFalse(g["videos"]["p3"]["visible"])                    # vidéo masquée avec son [role=group]
        self.assertEqual(g["videos"]["p3"]["wrap_display"], "none")
        for v in g["videos"].values():
            self.assertFalse(v["play_visible"], v)                       # commandes vidéo masquées
        self.assertFalse(g["actions"]["like"] or g["actions"]["bookmark"], g["actions"])

    def test_after_geometry_text_media_video(self):
        r = self.run_e2e(self.ext, "geom", "after")
        g = r["geom"]
        self.assertEqual(g["vw"], 1920)
        col = g["col"]
        self.assertEqual((col[0], col[2]), (240, 1440), col)              # marges sûres 240 px, centrée
        self.assertEqual(col[0], 1920 - col[0] - col[2])
        self.assertEqual((g["p1"][0], g["p1"][2]), (240, 1440), g["p1"])  # post = colonne entière
        self.assertEqual(r["geom_plain"], [240, 1440])                    # sans le plafond supposé : identique
        ln = g["lines"]
        self.assertEqual(ln["font"], "32px")
        self.assertGreaterEqual(ln["width"], 1250, ln)
        self.assertTrue(80 <= ln["chars_per_line"] <= 110, ln)           # lignes pleines, jamais des bouts
        ph = g["photo"]["box"]
        self.assertAlmostEqual(ph[2] / ph[3], 16 / 9, delta=0.02)         # photo sans déformation
        self.assertEqual(g["photo"]["bg"], ph)
        for vid, v in g["videos"].items():
            self.assertTrue(v["visible"], (vid, v))
            self.assertEqual((v["vw"], v["vh"]), (640, 360), (vid, v))
            self.assertGreaterEqual(v["ready"], 2, (vid, v))
            self.assertIsNone(v["err"], (vid, v))
            self.assertAlmostEqual(v["rect"][2] / v["rect"][3], 16 / 9, delta=0.02)
            self.assertLessEqual(v["rect"][3], 1080 - 96 - 80, (vid, v))  # tient dans l'écran
            for k in ("controls_visible", "play_visible", "mute_visible", "seek_visible"):
                self.assertTrue(v[k], (vid, k, v))                       # vraies commandes vidéo intactes
            self.assertEqual((v["paused"], v["t"], v["autoplay"]), (True, 0, False), (vid, v))   # rien lancé
            self.assertNotEqual(v["wrap_display"], "none")
            sh_ = r["video_shots"][vid]
            self.assertTrue(sh_["vis"]["hit_video"], (vid, sh_))          # rien ne la recouvre (aide comprise)
            self.assert_red(sh_["point"]["screen"], (vid, "capture"))     # première image à l'ÉCRAN
            self.assert_red(sh_["point"]["frame"], (vid, "image décodée"))
        a = g["actions"]
        # 0.7.15 : Partager (aucun marqueur propre) reste natif ; rangée citée jamais masquée en bloc
        self.assertEqual(a, {"like": True, "bookmark": True, "reply": False, "retweet": False, "share": True,
                             "views": False, "unlike_p2": True, "remove_p2": True, "quote_like": False,
                             "quote_reply": False, "quote_bookmark": False, "quote_bar": True})
        self.assertEqual(g["sidebar"], "none")
        self.assertGreater(g["lanes"], 0)
        self.assertIn("J'aime / Enregistrer", g["help"]["text"])

    def check_full(self, r):
        """Contrat télécommande ; renvoie la liste des écarts (vide = conforme) pour les mutants."""
        s = r["s"]
        bad = []

        def want(cond, what):
            if not cond:
                bad.append(what)
        want(s["initial"]["post"] is None and s["initial"]["acts"] == [], "initial")
        want(s["down1"]["post"] == "p1" and s["down1"]["act"] is None, "↓ post")
        for k, b in (("right1", "p1-like"), ("right2", "p1-bookmark"), ("right3", "p1-like")):
            want(s[k]["act"] == b and s[k]["act_is_active"] and s[k]["acting"], k)
            want(not s[k]["act_under_help"] and s[k]["act_in_view"], k + " visible hors aide")
        want(s["right1"]["label"] == '"J\'aime"' and s["right2"]["label"] == '"Enregistrer"', "libellés")
        want(s["right1"]["ring"] == "solid 6px rgb(255, 255, 255)" and s["right1"]["post_outline"] == "dashed",
             "focus distinct")
        want(s["synthetic"]["act"] == "p1-like" and s["focus_only_acts"] == 0, "focus seul / synthétiques : rien")
        want(s["ok1"]["acts"] == [{"post": "p1", "action": "like", "trusted": True}], "OK = UNE activation native")
        want(s["ok1"]["act_testid"] == "unlike" and s["ok1"]["label"] == '"Aimé · OK retire"', "état activé")
        want(len(s["ok_held"]["acts"]) == 2, "OK tenu : une activation par appui (%d)" % len(s["ok_held"]["acts"]))
        want(len(s["ok2"]["acts"]) == 3 and s["ok2"]["acts"][-1]["action"] == "like", "retrait")
        want(all(x["trusted"] for x in s["ok2"]["acts"]), "activations fiables seulement")
        want(s["left_exit"]["act"] is None and s["left_exit"]["active"] == "BODY" and s["left_exit"]["toast"] == "",
             "← sort sans partager")
        want(s["left_quick"]["toast"] == "", "← tenu / immédiat : aucun partage")
        want(s["left_share"]["toast"] == NOKEY, "← plus tard : partage")
        want(s["down_from_act"]["post"] == "p2" and s["down_from_act"]["act"] is None, "↓ depuis actions")
        want(s["p2_right"]["label"] == '"Aimé · OK retire"' and s["p2_right2"]["label"] == '"Enregistré · OK retire"',
             "état déjà activé lisible")
        want(s["p2_ok"]["act_testid"] == "bookmark" and s["p2_ok"]["act_is_active"] and s["p2_new_node"],
             "nœud remplacé : focus remis")
        want(s["p2_ok"]["acts"][-1] == {"post": "p2", "action": "removeBookmark", "trusted": True}, "retrait signet")
        # fenêtre de remplacement (focus retombé sur la page AVANT le refresh) : ← et OK immédiats sans effet
        want(s["race_left_kf"] == ["Enter:p2-removeBookmark", "ArrowLeft:BODY"], "fenêtre ← exercée")
        want(s["race_left_acts"] == 1 and s["race_left"]["toast"] == "" and s["race_left"]["url"] == "/home" and
             s["race_left_after"]["toast"] == "" and s["race_left_after"]["act"] is None,
             "remplacement + ← immédiat : aucun partage")
        want(s["race_ok_kf"] == ["Enter:p2-unlike", "Enter:BODY"], "fenêtre OK exercée")
        want(s["race_ok_acts"] == 1 and s["race_ok"]["url"] == "/home" and s["race_ok_after"]["url"] == "/home" and
             len(s["race_ok_after"]["acts"]) == len(s["race_ok"]["acts"]),
             "remplacement + OK immédiat : ni ouverture ni activation")
        want(s["cancel_left"]["toast"] == "" and s["cancel_left"]["act"] is None and s["p7_act"] == "p7-like",
             "refresh annulé + ← : aucun partage")
        want(s["cancel_up"]["post"] == "p6", "navigation normale après annulation")
        p3, p3r = s["p3"], s["p3_right"]
        want(p3["post"] == "p3" and p3["post_rect"][1] == 96 and p3["post_rect"][3] > 1080, "post long : début visible")
        want(p3r["act"] == "p3-like" and p3r["act_in_view"] and not p3r["act_under_help"], "post long : bouton amené")
        want(s["p3_up"]["act"] is None and s["p3_up"]["post"] == "p3", "↑ : quitte actions, lit le post long")
        want(s["p4_seq"] == ["p4-like", "p4-bookmark", "p4-like", "p4-bookmark"], "post cité jamais visé")
        want(s["p5_seq"] == ["p5-like", "p5-like"], "bouton absent")
        want(s["p6"]["post"] == "p6" and s["p6_right"]["act"] is None and s["p6_right"]["toast"] == NOACT, "aucune action")
        want(s["reuse_site_keys"] == 0 and s["reuse_acts_delta"] == 0 and s["reuse_ok"]["toast"] == STALE and s["reuse_ok"]["act"] is None,
             "DOM réutilisé : OK bloqué")
        want(s["open"]["url"] == "/zoe/status/99" and s["back"]["url"] == "/home", "ouvrir / retour")
        want(all(v["paused"] and v["t"] == 0 for v in s["videos_after"]), "vidéos jamais lancées")
        want(r["dialog"] == {"active": "notnow", "act": False, "post": False, "feed_help": False,
                             "dlg_display": "block", "acts": 0}, "fenêtre modale")
        want(r["login"] == {"tv": False, "lanes": 0, "act": False, "help": False, "sidebar": "block"}, "connexion")
        return bad

    def test_remote_like_bookmark_native_activation(self):
        r = self.run_e2e(self.ext, "full", "after")
        self.assertEqual(self.check_full(r), [], json.dumps(r["s"], ensure_ascii=False)[:4000])

    def test_mutants_detected(self):
        """Chaque garde est porteuse : la retirer fait échouer le contrat de bout en bout."""
        mutants = {
            "repeat": ("if (e.repeat || !actionValid(ae) || dialogOpen()) {", "if (!actionValid(ae) || dialogOpen()) {",
                       "OK tenu : une activation par appui"),
            "reuse": ("if (e.repeat || !actionValid(ae) || dialogOpen()) {", "if (e.repeat || dialogOpen()) {", "DOM réutilisé : OK bloqué"),
            "left_guard": ("performance.now() >= leftGuardUntil", "true", "← tenu / immédiat : aucun partage"),
            "race": ("if (actEl && ae !== actEl && !actionButton(ae)) {", "if (false) {",
                     "remplacement + ← immédiat : aucun partage"),
            "cancel_guard": ("if (!(keep && restoreAct())) { quitActions(); }",
                             "if (!(keep && restoreAct())) { exitActions(); }", "refresh annulé + ← : aucun partage"),
        }
        for name, (old, new, expect) in mutants.items():
            with self.subTest(mutant=name):
                d = tempfile.mkdtemp(prefix="otvxmut", dir="/tmp")
                self.addCleanup(shutil.rmtree, d, True)
                ext = os.path.join(d, "ext")
                shutil.copytree(self.ext, ext)
                src = read(os.path.join(ext, "x.js"))
                self.assertEqual(src.count(old), 1, name)
                with open(os.path.join(ext, "x.js"), "w", encoding="utf-8") as f:
                    f.write(src.replace(old, new))
                bad = self.check_full(self.run_e2e(ext, "full", "mut-" + name, shots=False))
                self.assertTrue(any(b.startswith(expect) for b in bad), (name, bad))


if __name__ == "__main__":
    unittest.main()
