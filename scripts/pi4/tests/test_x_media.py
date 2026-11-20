"""X TV 0.7.15 — aperçus de médias du fil ET du détail d'un post jamais masqués par la rangée d'actions ; bandeau d'aide
qui ne laisse plus voir de texte de post long à côté ou au travers.

Terrain 0.7.14 (Alex) : même cadre vide dans le fil et le détail ; cause non prouvée sur son post. Diagnostic local :
dans un groupe ancêtre MIXTE contenant J'aime, les règles CSS de 0.7.14 masquaient les descendants interactifs d'un
poster, d'une carte, d'une image ou d'un lecteur différés AVANT qu'un video/data-testid média n'apparaisse (et
display:none bloque l'IntersectionObserver qui les initialiserait). 0.7.15 : x.js marque la rangée (data-otvx-bar) ;
x.css ne masque QUE dans une rangée positivement identifiée (« row ») ; groupe mixte ou douteux : rien masqué.

- statique : aucune règle de masquage ne devine un [role=group] ; chaque masquage vise data-otvx-bar avec garde média ;
  x.js identifie la rangée la plus interne et ne retient que les boutons de CE post ; aide en bande opaque ;
- bout en bout (Chromium complet réel, extension installée par install.sh, fixtures LOCALES anonymes, toute autre
  requête annulée) : fil ET détail ; poster, carte, image différée, lecteur différé, média initialisé, portrait 9:16 ;
  rangée sœur, imbriquée dans un groupe mixte (2 profondeurs), boutons à plat dans le groupe mixte, post cité à média,
  bouton remplacé par X ; « Show more » et liens natifs. Preuves par pixel de la CAPTURE (PNG décodé) et chargement
  réel ; témoin SANS extension ; contrôle NÉGATIF 0.7.14 (commit 205b883) qui doit échouer ; mutants.
Limite : le groupe ancêtre mixte est une hypothèse de pire cas, non attestée sur le DOM réel de X ; la session X réelle
sur la Pi (Wayland/HDMI, vrai DOM) n'est PAS couverte ici (gate terrain).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests._common import PI4
from tests.test_x_feed import css_rules, no_comments
from tests.test_x_tv import EXT, EXT_DST, INSTALL, NODE, full_chrome, read, sh
from opentv_pi import VERSION, xcontext

XJS = os.path.join(EXT, "x.js")
XCSS = os.path.join(EXT, "x.css")
E2E = os.path.join(PI4, "tests", "e2e_x_media.js")
REPO = os.path.dirname(os.path.dirname(PI4))
NEGATIVE = "205b883"   # 0.7.14 déployée : contrôle négatif (doit reproduire les cadres vides)
GUARD = (':not(:has(img, video, audio, picture, canvas, iframe, object, embed, article, [data-testid="tweetPhoto"], '
         '[data-testid="videoPlayer"], [data-testid="videoComponent"], [data-testid="card.wrapper"], '
         '[data-testid="tweetText"]))')
STATES = ("poster", "card", "lazy", "defer", "init")
NESTED = ("sib", "grp", "deep")   # rangée [role=group] propre : nettoyée
FLAT = ("flat",)                  # boutons à plat dans le groupe mixte : rendu natif intact
OK_ON = ("poster_grp", "defer_deep", "init_flat", "card_flat", "rp", "pqm")
URLS = {"home": "/home", "status": "/fix_poster_sib/status/1"}   # OK sur un bouton : jamais une navigation
NOACT = "Ce post n'a pas de bouton J'aime ou Enregistrer visible."


class TestMediaStatic(unittest.TestCase):
    def test_css_hides_only_positively_marked_row(self):
        """Aucun masquage ne devine un [role=group] : chaque règle display:none du fil vise la rangée marquée par x.js
        (row, ou rangée pure d'un post cité), toujours avec la garde média. Aucun média ciblé, nulle part."""
        n = 0
        for sels, body in css_rules(read(XCSS)):
            for s in sels:
                self.assertTrue(s.startswith("html.otvx-tv"), s)
                if "display: none" in body:
                    self.assertNotIn('role="group"', s, s)
                    if "article" in s:
                        n += 1
                        self.assertRegex(s, r'\[data-otvx-bar="(row|quoted)"\]' + re.escape(GUARD), s)
                elif "data-otvx-bar" in s and "::after" not in s:
                    self.assertIn('[data-otvx-bar="row"]' + GUARD, s)    # mise en page : rangée pure seulement
                rest = s.replace(GUARD, "")
                for media in ("video", "videoPlayer", "videoComponent", "tweetPhoto", "img", "card"):
                    self.assertNotRegex(rest, r"\b%s\b" % media, s)
        self.assertEqual(n, 3)   # autres contrôles de la rangée, enveloppes sans J'aime/Enregistrer, rangée citée
        body = re.sub(r"/\*.*?\*/", "", read(XCSS), flags=re.S)
        self.assertNotIn('[data-otvx-bar="mixed"]', body)                    # groupe mixte : aucune règle
        for bad in ("object-fit", "max-height", "aspect-ratio", "url(", "@import"):
            self.assertNotIn(bad, body)

    def test_xjs_row_marking_and_actions(self):
        src = no_comments(read(XJS))
        ma = src[src.index("function markBars(on)"):src.index("function el(tag, cls, text)")]
        self.assertIn('want.set(r, pureRow(r, p) ? "row" : "mixed");', ma)
        self.assertIn('if (qr && !want.has(qr) && pureRow(qr, q)) { want.set(qr, "quoted"); }', ma)
        self.assertIn("if (!want.has(e)) { e.removeAttribute(BAR_ATTR); }", ma)
        ro = src[src.index("function rowOf(post)"):src.index("function rowControl(")]
        self.assertIn("ownerPost(b) === post", ro)                             # jamais un bouton d'un post cité
        self.assertIn("!gs.some((h) => h !== g && g.contains(h))", ro)         # le plus interne
        pr = src[src.index("function pureRow(g, post)"):src.index("function markBars(on)")]
        for need in ("g.querySelector(GRAPHIC_SEL)", 'getComputedStyle(n).backgroundImage !== "none"',
                     "!rowControl(n, post)", "nodes.length > BAR_NODES_MAX"):
            self.assertIn(need, pr)
        self.assertIn("return rowOf(post);", src[src.index("function actionBar(post)"):src.index("function actions(post)")])
        self.assertIn("ownerPost(b) === post && usable(b)", src)
        self.assertIn("markBars(on);", src[src.index("function refresh()"):])
        for bad in (".click(", "dispatchEvent", "fetch(", "sendBeacon", "innerHTML", "localStorage"):
            self.assertNotIn(bad, src.replace('const ACTION_TESTIDS = ["like", "unlike", "bookmark", "removeBookmark"];', ""))
        self.assertEqual(src.count("sendMessage("), 1)                         # partage (←) seulement : aucune télémétrie

    def test_help_is_opaque_full_width_band(self):
        rules = {tuple(s): b for s, b in css_rules(read(XCSS))}
        body = rules[("html.otvx-tv .otvx-help",)]
        for need in ("left: 0 !important", "right: 0 !important", "bottom: 0 !important", "transform: none !important",
                     "background: #0a0c10 !important", "pointer-events: none !important"):
            self.assertIn(need, body)
        self.assertNotIn("rgba(10, 12, 16", body)                               # plus de pastille translucide
        src = no_comments(read(XJS))
        self.assertIn("return h && h.height > 0 ? h.top - HELP_GAP : window.innerHeight - HELP_GAP;", src)
        self.assertEqual(src.count("window.scrollBy("), 6)                      # comme 0.7.14 : aucun défilement ajouté

    def test_version_and_docs(self):
        self.assertEqual(VERSION, "0.7.15")
        self.assertEqual(json.loads(read(os.path.join(EXT, "manifest.json")))["version"], VERSION)
        readme = read(os.path.join(PI4, "README.md"))
        agents = read(os.path.join(REPO, "AGENTS.md"))
        for doc in (readme, agents):
            for need in ("0.7.15", "data-otvx-bar", "mixte", "bande opaque"):
                self.assertIn(need, doc)


@unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT") and full_chrome(),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT + OPENTV_TEST_CHROME_FULL (Chromium complet) + node")
class TestMediaEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="otvxmedia", dir="/tmp")
        p = sh([INSTALL, "--root", cls.root, "--skip-preflight"])
        assert p.returncode == 0, p.stderr.decode()[-2000:]
        cls.ext = os.path.join(cls.root, EXT_DST.lstrip("/"))
        cls.shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvxmediashots", dir="/tmp")
        os.makedirs(cls.shots, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def run_e2e(self, ext, label, pages=("home", "status"), shots=True):
        cfg = {"chrome": full_chrome(), "ext": ext, "ext_id": xcontext.EXTENSION_ID, "label": label,
               "shots": self.shots if shots else "", "pages": list(pages)}
        p = subprocess.run([NODE, E2E, os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)], capture_output=True,
                           timeout=600)   # un seul Chromium à la fois
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-3000:] + p.stderr.decode()[-3000:])
        r = json.loads(p.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(r["requests"]["aborted"], [], r["requests"])          # aucune requête hors fixture
        return r

    @staticmethod
    def media_ids():
        return ["%s_%s" % (st, s) for st in STATES for s in NESTED + FLAT] + ["qm", "rp"]

    def check_media(self, r):
        """Aperçus : renvoie les écarts (vide = conforme), pour 0.7.15, le témoin sans extension et le négatif."""
        bad = []
        for kind, pg in r["pages"].items():
            for mid in self.media_ids():
                m = pg["posts"][mid]
                tag = "%s:%s" % (kind, mid)
                if not m["red"]:
                    bad.append("pixel " + tag)                                # cadre vide à l'écran
                if not m["loaded"]:
                    bad.append("chargement " + tag)                           # image/lecteur différé jamais chargé
                if m["hidden_in_frame"]:
                    bad.append("masqué " + tag)
                if not all(p["hit"] and (p["hit"].startswith("hit-") or p["hit"].startswith("img-")) for p in m["points"]):
                    bad.append("lien natif " + tag)                           # zone native du média atteignable
                if mid != "qm" and not m["more_vis"]:
                    bad.append("show more " + tag)
                if (mid.startswith("poster") or mid.startswith("init") or mid.startswith("defer") or mid == "rp") \
                        and not m["play_vis"]:
                    bad.append("▶ " + tag)
        return bad

    def check_rows(self, r):
        bad = []
        for kind, pg in r["pages"].items():
            for mid in self.media_ids():
                m, tag = pg["posts"][mid], "%s:%s" % (kind, mid)
                if mid == "qm":
                    if m["bar"] != "quoted" or m["like_vis"]:
                        bad.append("rangée citée " + tag)                     # rangée pure du post cité : masquée
                elif mid.split("_")[-1] in NESTED:
                    if not (m["bar"] == "row" and m["like_vis"] and m["bookmark_vis"] and not m["reply_vis"]
                            and not m["share_vis"] and not m["views_vis"]):
                        bad.append("rangée pure " + tag)                      # 0.7.14 conservé : J'aime/Enregistrer
                else:
                    if not (m["outer"] == "mixed" and m["bar"] is None and m["reply_vis"] and m["like_vis"]
                            and m["bookmark_vis"]):
                        bad.append("mixte natif " + tag)                      # groupe mixte : rien masqué
        return bad

    def check_dpad(self, r):
        bad = []
        for kind, pg in r["pages"].items():
            ids = [m for m in self.media_ids() if m not in ("qm", "rp")] + ["pqm", "pqp", "rp"]
            for pid in ids:
                d, tag = pg["dpad"].get(pid), "%s:%s" % (kind, pid)
                if not d or not d["reached"]:
                    bad.append("↓ " + tag)
                    continue
                if d["seq"] != [pid + "-like", pid + "-bookmark"] or not d["active_ok"] or d["toast"]:
                    bad.append("→ " + tag)                                    # vrais boutons de CE post, focus natif
                if d["left_toast"] or d["left_act"] is not None:
                    bad.append("← " + tag)
                if pid in OK_ON:
                    a = d.get("after_ok") or {}
                    if d.get("ok_acts") != [{"post": pid, "btn": pid + "-like", "action": "like", "trusted": True}]:
                        bad.append("OK " + tag)                               # UNE activation native, post parent
                    if not (a.get("act") == pid + "-like" and a.get("act_testid") == "unlike" and a.get("act_is_active")
                            and a.get("url") == URLS[kind]):
                        bad.append("après OK " + tag)
                    if pid == "rp" and not a.get("new_node"):
                        bad.append("remplacement " + tag)                     # X a remplacé le bouton : focus remis
        return bad

    def check_help(self, r):
        bad = []
        for kind, pg in r["pages"].items():
            h = pg["help"]
            if not (h["focus"] == "long" and h["lines_behind_help"] >= 1 and h["sampled"] > 1000):
                bad.append("aide: scène " + kind)                             # le test exerce un vrai recouvrement
            if h["max_lum_text_above"] < 150:
                bad.append("aide: témoin pixel " + kind)                      # le décodeur voit bien le texte
            if h["max_lum_under_help"] > 40:
                bad.append("aide: texte sous l'aide " + kind)
            if h["scroll_after_refresh"] != h["scroll0"]:
                bad.append("aide: saut " + kind)
            if not (h["down_post"] == "long" and h["revealed"]):
                bad.append("aide: ↓ révèle " + kind)
        return bad

    def test_after_0715_feed_and_detail(self):
        r = self.run_e2e(self.ext, "after")
        self.assertEqual(sorted(r["pages"]), ["home", "status"])
        bad = self.check_media(r) + self.check_rows(r) + self.check_dpad(r) + self.check_help(r)
        self.assertEqual(bad, [], json.dumps(r["pages"], ensure_ascii=False)[:4000])
        for kind in ("home", "status"):
            self.assertEqual(r["pages"][kind]["help"]["help"][0::2], [0, 1920])   # bande pleine largeur

    def test_witness_without_extension(self):
        r = self.run_e2e("", "off")
        self.assertEqual(self.check_media(r), [])                             # la fixture rend tout nativement

    def test_negative_0714_reproduces_empty_frames(self):
        if not shutil.which("git") or subprocess.run(["git", "-C", REPO, "cat-file", "-e", NEGATIVE + "^{commit}"],
                                                     capture_output=True).returncode:
            self.skipTest("historique git %s absent" % NEGATIVE)
        d = tempfile.mkdtemp(prefix="otvx0714", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        arc = subprocess.run(["git", "-C", REPO, "archive", NEGATIVE, "scripts/pi4/x-tv-extension"],
                             capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=arc, check=True)
        ext = os.path.join(d, "scripts", "pi4", "x-tv-extension")
        self.assertEqual(json.loads(read(os.path.join(ext, "manifest.json")))["version"], "0.7.14")
        r = self.run_e2e(ext, "neg0714", pages=("home",))
        media = self.check_media(r)
        for st in ("poster", "card", "lazy", "defer"):
            for s in ("grp", "deep", "flat"):
                self.assertIn("pixel home:%s_%s" % (st, s), media)            # cadre vide reproduit
        for mid in ("lazy_grp", "defer_grp", "defer_deep"):
            self.assertIn("chargement home:" + mid, media)                     # IntersectionObserver bloqué
        self.assertIn("pixel home:qm", media)
        for mid in ("poster_sib", "card_sib", "lazy_sib", "defer_sib", "init_sib", "init_grp", "init_deep"):
            self.assertNotIn("pixel home:" + mid, media)                       # le témoin est discriminant
        self.assertIn("→ home:init_flat", self.check_dpad(r))                   # groupe mixte + vidéo : aucun bouton
        self.assertEqual(r["pages"]["home"]["dpad"]["init_flat"]["toast"], NOACT)
        self.assertIn("aide: texte sous l'aide home", self.check_help(r))

    def test_mutants_detected(self):
        """Chaque garde porte : la retirer fait échouer le contrat (page du fil seulement)."""
        mutants = {
            "pure_always": ("x.js", "function pureRow(g, post) {", "function pureRow(g, post) { return true;",
                            "pixel home:poster_flat"),
            "outermost": ("x.js", "return gs.find((g) => !gs.some((h) => h !== g && g.contains(h))) || null;",
                          "return gs.find((g) => !gs.some((h) => h !== g && h.contains(g))) || null;",
                          "rangée pure home:poster_grp"),
            "media_exclusion": ("x.js", "return rowOf(post);   //",
                                "return own(post, '[role=\"group\"]').find((g) => g.querySelector(ACTION_SEL) && "
                                "!g.querySelector(MEDIA_SEL)) || null;   //", "→ home:init_flat"),
            "pill_help": ("x.css", "background: #0a0c10 !important;", "background: rgba(10, 12, 16, 0.5) !important;",
                          "aide: texte sous l'aide home"),
        }
        for name, (f, old, new, expect) in mutants.items():
            with self.subTest(mutant=name):
                d = tempfile.mkdtemp(prefix="otvxmut", dir="/tmp")
                self.addCleanup(shutil.rmtree, d, True)
                ext = os.path.join(d, "ext")
                shutil.copytree(self.ext, ext)
                src = read(os.path.join(ext, f))
                self.assertEqual(src.count(old), 1, name)
                with open(os.path.join(ext, f), "w", encoding="utf-8") as fh:
                    fh.write(src.replace(old, new))
                r = self.run_e2e(ext, "mut-" + name, pages=("home",), shots=False)
                bad = self.check_media(r) + self.check_rows(r) + self.check_dpad(r) + self.check_help(r)
                self.assertIn(expect, bad, (name, bad))


if __name__ == "__main__":
    unittest.main()
