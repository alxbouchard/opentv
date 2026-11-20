"""X TV 0.7.15 — aperçus de médias du fil ET du détail d'un post jamais masqués par la rangée d'actions ; bandeau d'aide
qui ne laisse plus voir de texte de post long à côté ou au travers.

Terrain 0.7.14 (Alex) : même cadre vide dans le fil et le détail ; cause non prouvée sur son post. Diagnostic local :
dans un groupe ancêtre MIXTE contenant J'aime, les règles CSS de 0.7.14 masquaient les descendants interactifs d'un
poster, d'une carte, d'une image ou d'un lecteur différés AVANT qu'un video/data-testid média n'apparaisse (et
display:none bloque l'IntersectionObserver qui les initialiserait). Revue de c31a0c7 : une « rangée pure » jugée par
x.js acceptait encore un div nu, un texte libre ou un fond différé, puis x.css masquait l'enfant ou tout le groupe cité.
0.7.15 (corrigée) : AUCUN conteneur jugé ni masqué — x.css masque seulement, un par un, des contrôles identifiés par
eux-mêmes (Répondre, Republier, statistiques ; boutons d'un post cité). Partager (sans marqueur propre) reste natif.

- statique : la liste exacte des masquages (test_x_feed.HIDDEN_CONTROLS), aucun data-otvx-bar, x.js lecture seule pour
  la rangée (la plus interne, boutons de CE post) ; aide en bande opaque ;
- bout en bout (Chromium complet réel, extension installée par install.sh, fixtures LOCALES anonymes, toute autre
  requête annulée) : fil ET détail ; poster, carte, image différée, lecteur différé, média initialisé, portrait 9:16 ;
  div nu, texte libre et fond différé SANS attribut ; rangée sœur, imbriquée dans un groupe mixte (2 profondeurs),
  boutons à plat dans le groupe mixte, post parent et post cité, bouton remplacé par X ; « Show more » et liens natifs.
  Preuves par pixel de la CAPTURE (PNG décodé) et chargement réel ; témoin SANS extension ; contrôles NÉGATIFS 0.7.14
  (205b883) et 0.7.15 initiale (c31a0c7) qui doivent échouer ; mutants.
Revue de 2f4c74d : la vieille règle x.css « [data-testid=placementTracking] » de la colonne principale (enveloppe
générique, ancêtre possible d'un lecteur, même avant son initialisation) est RETIRÉE sans heuristique de remplacement ;
l'assertion statique couvre TOUS les display:none (liste exacte, avec ou sans « article »). Fixture : espaceur portrait +
placementTracking > videoPlayer > videoComponent > video (ptinit) et la même enveloppe SANS video, lecteur différé
(ptdefer) ; fil + détail ; pixel, chargement, dimensions du cadre, ancêtres jamais masqués ; contrôle négatif 2f4c74d.
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
from tests.test_x_feed import ALL_HIDDEN, HIDDEN_CONTROLS, NEVER_HIDDEN_TOKENS, PAGE_CHROME, css_rules, no_comments
from tests.test_x_tv import EXT, EXT_DST, INSTALL, NODE, full_chrome, read, sh
from opentv_pi import VERSION, xcontext

XJS = os.path.join(EXT, "x.js")
XCSS = os.path.join(EXT, "x.css")
E2E = os.path.join(PI4, "tests", "e2e_x_media.js")
REPO = os.path.dirname(os.path.dirname(PI4))
NEGATIVE = "205b883"   # 0.7.14 déployée : contrôle négatif (doit reproduire les cadres vides)
REJECTED = "c31a0c7"   # 0.7.15 initiale, refusée en revue : div nu / texte libre / fond différé masqués
PT_NEG = "2f4c74d"     # 0.7.15 révisée, refusée en revue : enveloppe placementTracking masquée (lecteur caché/bloqué)
PT = ("ptinit_sib", "ptdefer_sib")                   # lecteur dans une enveloppe placementTracking (initialisé, différé)
STATES = ("poster", "card", "lazy", "defer", "init", "bare", "text", "bgl")
UNMARKED = ("bare", "text", "bgl")                    # aperçus SANS attribut (revue de c31a0c7)
QUOTES = ("qm", "qbare", "qtext", "qbgl")             # posts cités dont l'aperçu partage le groupe des boutons
NESTED = ("sib", "grp", "deep")   # rangée [role=group] propre : nettoyée
FLAT = ("flat",)                  # boutons à plat dans le groupe mixte : rendu natif intact
OK_ON = ("poster_grp", "defer_deep", "init_flat", "card_flat", "rp", "pqm") + PT
PARENTS = ("pqm", "pqbare", "pqtext", "pqbgl", "pqp", "rp")
URLS = {"home": "/home", "status": "/fix_poster_sib/status/1"}   # OK sur un bouton : jamais une navigation
NOACT = "Ce post n'a pas de bouton J'aime ou Enregistrer visible."


class TestMediaStatic(unittest.TestCase):
    def test_css_hides_only_individual_controls(self):
        """Chaque display:none dans un post vise UN contrôle identifié par lui-même ; jamais un [role=group], une
        enveloppe, un enfant « sans J'aime », un :has() ni une rangée marquée. Aucun média ciblé, nulle part.
        Revue de 2f4c74d : vérifié pour TOUS les sélecteurs de masquage, qu'ils nomment « article » ou non ; les autres
        (interface de page) sont une liste exacte, exclue de l'intérieur d'un post ; aucune enveloppe générique."""
        hides = []
        for sels, body in css_rules(read(XCSS)):
            for s in sels:
                self.assertTrue(s.startswith("html.otvx-tv"), s)
                self.assertNotIn("data-otvx-bar", s)
                if re.search(r"display\s*:\s*none", body):
                    hides.append(s)
                    for tok in NEVER_HIDDEN_TOKENS:
                        self.assertNotIn(tok, s.replace('[data-testid="primaryColumn"] ', ""), (tok, s))
                    if s in HIDDEN_CONTROLS:
                        for bad in ('role="group"', ":has(", ":not(", ">", "div", "~", "+"):
                            self.assertNotIn(bad, s.replace("html.otvx-tv ", ""), (bad, s))
                    elif s in PAGE_CHROME:
                        self.assertTrue(s.endswith(":not(article *)"), s)   # jamais dans un post
                        self.assertEqual(s.count("article"), 1, s)
        self.assertEqual(tuple(hides), ALL_HIDDEN)
        self.assertNotIn("placementTracking", no_comments(read(XCSS)))
        body = re.sub(r"/\*.*?\*/", "", read(XCSS), flags=re.S)
        for bad in ("object-fit", "max-height", "aspect-ratio", "url(", "@import", "data-otvx-bar"):
            self.assertNotIn(bad, body)

    def test_xjs_row_is_read_only(self):
        """x.js ne juge ni ne marque aucune rangée : rowOf (la plus interne, boutons de CE post) sert au D-pad seul."""
        src = no_comments(read(XJS))
        for gone in ("markBars", "pureRow", "rowControl", "BAR_ATTR", "data-otvx-bar", "GRAPHIC_SEL", "backgroundImage"):
            self.assertNotIn(gone, src)
        ro = src[src.index("function rowOf(post)"):src.index("function el(tag, cls, text)")]
        self.assertIn("ownerPost(b) === post", ro)                             # jamais un bouton d'un post cité
        self.assertIn("!gs.some((h) => h !== g && g.contains(h))", ro)         # le plus interne
        self.assertNotIn("setAttribute", ro)
        self.assertEqual(set(re.findall(r'setAttribute\(([A-Z_]+)', src)), {"LANE_ATTR", "ACT_ATTR", "ACTING_ATTR"})
        self.assertIn("return rowOf(post);", src[src.index("function actionBar(post)"):src.index("function actions(post)")])
        self.assertIn("ownerPost(b) === post && usable(b)", src)
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
            for need in ("0.7.15", "un par un", "c31a0c7", "bande opaque"):
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
        return ["%s_%s" % (st, s) for st in STATES for s in NESTED + FLAT] + list(PT) + list(QUOTES) + ["rp"]

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
                if m["hidden_ancestors"]:
                    bad.append("ancêtre masqué " + tag)                       # aucun masquage générique au-dessus
                f = m["frame"]                                                # cadre portrait 9:16 à sa vraie taille
                if not (m["frame_vis"] and f and f[2] >= 300 and abs((f[3] - 2) - (f[2] - 2) * 16 / 9) <= 3):
                    bad.append("dimensions " + tag)
                if mid in PT and not (m["player"] and m["media_tag"] == "VIDEO" and m["media_vis"]
                                      and m["pt"] and m["pt"]["display"] != "none" and m["pt"]["vis"]):
                    bad.append("lecteur " + tag)                              # lecteur initialisé ET affiché
                native = ("hit-", "img-", "in-frame") if (mid.split("_")[0] in ("text", "bgl") or mid in ("qtext", "qbgl")) \
                    else ("hit-", "img-")                                     # aperçu sans attribut : son propre nœud
                if not (m["points"] and all(p["hit"] and p["hit"].startswith(native) for p in m["points"])):
                    bad.append("lien natif " + tag)                           # zone native du média atteignable
                if mid not in QUOTES and not m["more_vis"]:
                    bad.append("show more " + tag)
                if (mid.split("_")[0] in ("poster", "init", "defer", "bare", "ptinit", "ptdefer")
                        or mid in ("rp", "qm", "qbare")) \
                        and not m["play_vis"]:
                    bad.append("▶ " + tag)
        return bad

    def check_rows(self, r):
        """Contrôles : J'aime / Enregistrer du post visibles ; Répondre / statistiques masqués UN PAR UN ; Partager
        (aucun marqueur propre) natif ; post cité : ses boutons masqués un par un, son groupe jamais ; aucun marquage."""
        bad = []
        for kind, pg in r["pages"].items():
            for mid in self.media_ids():
                m, tag = pg["posts"][mid], "%s:%s" % (kind, mid)
                if m["bar"] is not None or m["outer"] is not None:
                    bad.append("marquage " + tag)                             # aucune rangée jugée ni marquée
                if mid in QUOTES:
                    if m["like_vis"] or m["bookmark_vis"] or m["reply_vis"] or m["views_vis"] or not m["share_vis"]:
                        bad.append("rangée citée " + tag)                     # boutons du cité un par un, groupe intact
                elif not (m["like_vis"] and m["bookmark_vis"]):
                    bad.append("J'aime/Enregistrer " + tag)
                elif m["reply_vis"] or m["views_vis"] or not m["share_vis"]:
                    bad.append("secondaire " + tag)
        return bad

    def check_dpad(self, r):
        bad = []
        for kind, pg in r["pages"].items():
            ids = [m for m in self.media_ids() if m not in QUOTES and m != "rp"] + list(PARENTS)
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

    def test_negative_c31a0c7_hides_unmarked_previews(self):
        """Témoin de la revue : la 0.7.15 initiale (c31a0c7) masque div nu, texte libre et fond différé (post parent à
        plat et post cité), fil ET détail ; les mêmes aperçus dans une rangée sœur/imbriquée restent visibles."""
        if not shutil.which("git") or subprocess.run(["git", "-C", REPO, "cat-file", "-e", REJECTED + "^{commit}"],
                                                     capture_output=True).returncode:
            self.skipTest("historique git %s absent" % REJECTED)
        d = tempfile.mkdtemp(prefix="otvxc31", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        arc = subprocess.run(["git", "-C", REPO, "archive", REJECTED, "scripts/pi4/x-tv-extension"],
                             capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=arc, check=True)
        ext = os.path.join(d, "scripts", "pi4", "x-tv-extension")
        self.assertIn("function pureRow(g, post)", read(os.path.join(ext, "x.js")))
        r = self.run_e2e(ext, "negc31", shots=False)
        media = self.check_media(r)
        for kind in ("home", "status"):
            for st in UNMARKED:
                self.assertIn("pixel %s:%s_flat" % (kind, st), media)          # aperçu masqué (enfant du groupe)
                self.assertIn("pixel %s:q%s" % (kind, st), media)              # groupe cité masqué en bloc
                for s in NESTED:
                    self.assertNotIn("pixel %s:%s_%s" % (kind, st, s), media)  # témoin discriminant
            for mid in ("bare_flat", "bgl_flat", "qbare", "qbgl"):
                self.assertIn("chargement %s:%s" % (kind, mid), media)         # IntersectionObserver bloqué

    def test_negative_2f4c74d_hides_placement_tracking(self):
        """Témoin de la revue : 2f4c74d masque l'enveloppe placementTracking -> lecteur initialisé caché (cadre aux
        bonnes dimensions mais vide) et lecteur différé jamais initialisé, fil ET détail ; le même lecteur sans
        enveloppe (init_sib, defer_sib) reste visible (témoin discriminant)."""
        if not shutil.which("git") or subprocess.run(["git", "-C", REPO, "cat-file", "-e", PT_NEG + "^{commit}"],
                                                     capture_output=True).returncode:
            self.skipTest("historique git %s absent" % PT_NEG)
        d = tempfile.mkdtemp(prefix="otvxpt", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        arc = subprocess.run(["git", "-C", REPO, "archive", PT_NEG, "scripts/pi4/x-tv-extension"],
                             capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=arc, check=True)
        ext = os.path.join(d, "scripts", "pi4", "x-tv-extension")
        self.assertIn('[data-testid="placementTracking"]', read(os.path.join(ext, "x.css")))
        r = self.run_e2e(ext, "negpt", shots=False)
        media = self.check_media(r)
        for kind in ("home", "status"):
            for mid in PT:
                self.assertIn("pixel %s:%s" % (kind, mid), media)                # cadre vide à l'écran
                self.assertIn("lecteur %s:%s" % (kind, mid), media)
                self.assertNotIn("dimensions %s:%s" % (kind, mid), media)        # même cadre, bonne taille
                self.assertEqual(r["pages"][kind]["posts"][mid]["pt"]["display"], "none")
            self.assertIn("chargement %s:ptdefer_sib" % kind, media)              # IntersectionObserver bloqué
            self.assertFalse(r["pages"][kind]["posts"]["ptdefer_sib"]["player"])
            for mid in ("init_sib", "defer_sib", "poster_sib"):
                self.assertFalse([b for b in media if b.endswith(" %s:%s" % (kind, mid))], (mid, media))   # discriminant

    def test_mutants_detected(self):
        """Chaque garde porte : la retirer fait échouer le contrat (page du fil seulement)."""
        col = 'html.otvx-tv [data-testid="primaryColumn"] '
        mutants = {
            "container_hide": ("x.css", col + 'article a[href*="/status/"][href$="/analytics"],',
                               col + 'article [role="group"] > :not(:has([data-testid="like"], '
                               '[data-testid="bookmark"])),', "pixel home:text_flat"),
            "quoted_group": ("x.css", col + 'article article :is(button, [role="button"]):is(',
                             col + 'article article [role="group"]:has([data-testid="like"]), ' + col +
                             'article article :is(button, [role="button"]):is(', "chargement home:qbgl"),
            "quoted_buttons": ("x.css", 'article article :is(button, [role="button"]):is(',
                               'article article :is(nothing):is(', "rangée citée home:qm"),
            "secondary": ("x.css", '[data-testid="reply"], [data-testid="retweet"]',
                          '[data-testid="none"], [data-testid="retweet"]', "secondaire home:poster_sib"),
            "media_exclusion": ("x.js", "return rowOf(post);   //",
                                "return own(post, '[role=\"group\"]').find((g) => g.querySelector(ACTION_SEL) && "
                                "!g.querySelector('video')) || null;   //", "→ home:init_flat"),
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
