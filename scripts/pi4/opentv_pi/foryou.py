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

    def tv_session(self):
        """Sélection figée pour l'écran TV (loopback) : rangées, affiches https, marques d'état."""
        s = self.tv.session(self.items())
        s["data"]["marks"] = dict(s["nav"].marks)
        return s

    def tv_feedback(self, session, uid, action):
        if not isinstance(action, str) or action not in self.engine.ACTIONS or \
                not isinstance(uid, str) or uid not in session.get("by_uid", {}):
            return None, "Action refusée."
        state, msg = self.tv.act(session, uid, action)
        if state is not None:
            session["nav"].marks[uid] = state
            session["data"]["marks"][uid] = state
        return state, msg
