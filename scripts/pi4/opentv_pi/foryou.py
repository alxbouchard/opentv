"""Adaptateur du moteur « Pour toi » existant (scripts/nanopi/opentv_foryou.py), chargé par chemin explicite.

Aucune logique de recommandation n'est copiée ici :
- outils agents  -> CLI du moteur `opentv_foryou.main([...])` exécutée en mémoire (bornes, refus d'URL, scrub) ;
- vue TV         -> `opentv_foryou.Bridge.session/act` (sélection figée, disjoncteur SQLite, messages français).
Le catalogue est lu par `opentv_bridge.load_catalogue_meta` (même validation que le NanoPi).
"""
import importlib.util
import io
import json
import os
import time

ENGINE_FILES = ("opentv_foryou.py", "opentv_bridge.py")


class EngineError(Exception):
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_engine(engine_dir):
    for f in ENGINE_FILES:
        if not os.path.isfile(os.path.join(engine_dir, f)):
            raise EngineError("missing_" + f.split(".")[0])
    try:
        fy = _load("opentv_pi_foryou_engine", os.path.join(engine_dir, "opentv_foryou.py"))
        br = _load("opentv_pi_foryou_catalogue", os.path.join(engine_dir, "opentv_bridge.py"))
    except (OSError, ImportError, SyntaxError) as e:
        raise EngineError(type(e).__name__)
    for attr in ("main", "Bridge", "scrub", "ACTIONS", "valid_source"):
        if not hasattr(fy, attr):
            raise EngineError("incompatible_engine")
    return fy, br


class ForYou:
    def __init__(self, engine_dir, state_dir, catalogue_file, source="films", clock=time.time, log=None):
        self.engine, self.bridge_mod = load_engine(engine_dir)
        if not self.engine.valid_source(source):
            raise ValueError("bad_source_id")
        self.clock, self.source, self.catalogue_file = clock, source, catalogue_file
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.db_path = os.path.join(state_dir, "foryou.sqlite3")
        self.env = {"HOME": state_dir, "STATE_DIR": state_dir, "CATALOGUE_FILE": catalogue_file,
                    "FORYOU_DB": self.db_path, "FORYOU_SOURCE_ID": source}
        self.detail_re = self.bridge_mod.Config({}).detail_re
        self.tv = self.engine.Bridge(self.db_path, source, log or (lambda *a, **k: None), clock=clock)

    def close(self):
        self.tv.reset()

    def scrub(self, obj):
        return self.engine.scrub(obj)

    def cli(self, argv):
        """-> (code de sortie, objet JSON) de la CLI agent du moteur, sans processus ni shell."""
        out = io.StringIO()
        code = self.engine.main(["opentv_foryou.py"] + [str(a) for a in argv], env=dict(self.env), now=self.clock(),
                                out=out)
        try:
            return code, json.loads(out.getvalue())
        except ValueError:
            return 3, {"ok": False, "error": "engine_output"}

    def items(self):
        items, _, _ = self.bridge_mod.load_catalogue_meta(self.catalogue_file, self.detail_re)
        return items

    def catalogue_page(self, limit, offset):
        """Page déterministe du catalogue accepté pour les agents locaux.

        Le moteur canonique construit les objets publics (identifiant stable, titre, année, genres et notes séparées)
        et son scrubber enlève toute adresse imbriquée. Les chemins de fiche/affiche et les champs bruts ne sortent
        jamais. La pagination évite qu'un agent transforme l'Agent Port en export JSON sans borne.

        Chaque élément porte ses marques publiques (`marks` : opinion, seen, watchlist, not_tonight), relues dans le
        profil local AU MOMENT de l'appel (jamais la sélection TV figée), pour qu'un agent qui parcourt le catalogue
        ne propose pas un film qu'Alex a déjà vu. Profil illisible (base verrouillée/corrompue) : `marks` = null et
        `store` = "unavailable" — jamais un faux « pas vu ».
        """
        profile = self.tv.call("profile")
        items, generated_at, _ = self.bridge_mod.load_catalogue_meta(self.catalogue_file, self.detail_re)
        rows = []
        for item in items:
            uid = self.engine.uid_for(self.source, item)
            title = self.engine.clean_text(item.get("title"), 200)
            if uid and title:
                rows.append((self.engine.norm(title), uid, item))
        rows.sort(key=lambda x: (x[0], x[1]))
        sel = rows[offset:offset + limit]
        marks = self.public_marks(profile, [uid for _t, uid, _i in sel]) if profile is not None else {}
        page = []
        for _title, uid, item in sel:
            public = self.engine.public_item(item, uid, [], None)
            public["description"] = self.engine.clean_text(item.get("description"), 300)
            public["marks"] = marks.get(uid)
            page.append(public)
        return self.scrub({"items": page, "offset": offset, "limit": limit, "total": len(rows),
                           "has_more": offset + limit < len(rows), "generated_at": generated_at,
                           "store": "ok" if profile is not None else "unavailable"})

    def public_marks(self, profile, uids):
        """uid -> marques publiques bornées {opinion, seen, watchlist, not_tonight} (aucun horodatage ni champ brut)."""
        out = {}
        for uid, m in self.engine.marks_for(profile or {}, uids, self.clock()).items():
            op = m.get("opinion")
            out[uid] = {"opinion": op if op in ("like", "dislike") else None, "seen": bool(m.get("seen")),
                        "watchlist": bool(m.get("watchlist")), "not_tonight": bool(m.get("not_tonight"))}
        return out

    def tv_session(self):
        """Sélection figée pour l'écran TV (loopback) : rangées, affiches https, marques d'état."""
        s = self.tv.session(self.items())
        s["data"]["marks"] = dict(s["nav"].marks)
        return s

    def tv_item(self, session, uid):
        """Fiche d'UN film du catalogue accepté pour la page TV locale (proposition d'un assistant), même forme que
        les cartes « Pour toi » : objet public du moteur + affiche https approuvée + résumé sans adresse + marques
        explicites. None si l'identifiant n'est pas dans le catalogue accepté de cette sélection."""
        x = (session.get("by_uid") or {}).get(uid) if isinstance(uid, str) else None
        if not isinstance(x, dict) or not isinstance(x.get("title"), str):
            return None
        pub = self.engine.public_item(x, uid, [], None)
        poster = x.get("poster")
        pub["poster"] = poster if isinstance(poster, str) and poster.startswith("https://") and len(poster) < 600 \
            else None
        desc = " ".join(self.engine.clean_text(x.get("description")).split())[:400]   # = ui_data (_strip_urls)
        pub["description"] = desc or None
        # marques relues dans le profil À CET INSTANT : un avis donné entre-temps par un agent (« déjà vu » dit à
        # Muse) n'est pas dans la sélection figée de la TV ; repli sur celle-ci seulement si le profil est illisible
        profile = self.tv.call("profile")
        if profile is not None:
            mark = self.public_marks(profile, [uid])[uid]
        else:
            mark = session["nav"].marks.get(uid) or dict(self.engine.EMPTY_MARK)
        pub["marks"] = dict(mark)
        return pub

    def tv_feedback(self, session, uid, action):
        if not isinstance(action, str) or action not in self.engine.ACTIONS or \
                not isinstance(uid, str) or uid not in session.get("by_uid", {}):
            return None, "Action refusée."
        state, msg = self.tv.act(session, uid, action)
        if state is not None:
            session["nav"].marks[uid] = state
            session["data"]["marks"][uid] = state
        return state, msg
