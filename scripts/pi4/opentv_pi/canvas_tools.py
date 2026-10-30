"""Outils Canvas FACULTATIFS de l'Agent Port (scope « canvas ») : proposer une ambiance OpenTV, jamais la publier.

- Visibles (liste MCP) et appelables SEULEMENT par une identité authentifiée qui porte « canvas » : l'Agent Port relit
  les droits à CHAQUE requête (liste comme appel) ; une révocation s'applique donc à l'appel suivant d'une session
  ouverte. `tools.TOOLS` (11 outils) et les 11 commandes du pont Muse restent inchangés : ce catalogue est à part.
- Arguments PLATS seulement : un préréglage connu et/ou des valeurs énumérées du schéma Canvas. Jamais de scène brute,
  de `row_order`, de texte, d'URL, de CSS ni de balisage (schéma fermé, validé ici puis par le magasin).
- Aucun outil ne confirme, n'applique, ne revient en arrière, ne réinitialise ni ne touche aux Réglages, au QR, à la
  lecture ou à la navigation : la feuille de la TV (« Non » focalisé) reste la seule publication. Un agent ne retire
  que SON aperçu (identité authentifiée exacte) et ne voit jamais celui d'un autre.
- Délai de grâce par agent CANONIQUE (« muse-device » -> « muse ») après un refus du propriétaire (« Non », échéance)
  ou un geste humain qui annule son aperçu : aucune proposition ne rouvre la feuille avant REFUSAL_COOLDOWN_S. Le
  retrait par l'agent lui-même n'en ouvre pas (débits agentport.TOOL_RATES seulement). Une proposition déjà en vol
  au moment exact du refus peut encore aboutir une fois (fenêtre bornée par le débit 1/5 s).
- Audit : l'App audite chaque proposition qu'elle traite (outil « canvas », action « proposed ») ; l'Agent Port
  n'enregistre donc canvas_preview que pour les refus en amont (scope, paramètres, débit, délai de grâce).
"""
import collections
import threading

from . import agents, tools
from .canvas_schema import ENUMS, PRESETS, CanvasError

SCOPE = "canvas"
FLAT_FIELDS = tuple(ENUMS)          # wallpaper, motion, accent, shade, hero_focus (jamais row_order)
REFUSAL_COOLDOWN_S = 120.0
MAX_TRACKED = 64                    # agents canoniques suivis (identités authentifiées seulement)
CONFIRMATION = "owner_on_tv"        # seule voie de publication : « Oui » du propriétaire sur la TV
SELF_AUDITED = frozenset({"canvas_preview"})
# codes du magasin -> codes STABLES renvoyés à l'agent (ensemble fermé ; tout le reste : store_unavailable)
CODES = {"ui_busy": "tv_busy", "busy_overflow": "tv_busy", "preview_pending": "preview_pending",
         "no_change": "no_change", "unknown_preview": "unknown_preview", "preview_expired": "unknown_preview",
         "invalid_scene": "invalid_params", "invalid_agent": "invalid_params"}
_FIELD_DESC = {"wallpaper": "Fond animé", "motion": "Mouvement (off = immobile)", "accent": "Couleur d'accent",
               "shade": "Voile sur le fond", "hero_focus": "Mise en avant du film choisi"}


def _enum(values, desc):
    return {"type": "string", "enum": list(values), "description": desc}


TOOLS = [
    {"name": "canvas_status", "scope": SCOPE, "title": "Ambiance OpenTV : état",
     "description": "Ambiance OpenTV publiée (préréglage et valeurs énumérées), TA proposition en attente éventuelle, "
                    "délai avant une nouvelle proposition (cooldown_s) et valeurs permises. Aucune adresse ni secret.",
     "inputSchema": tools._obj({})},
    {"name": "canvas_preview", "scope": SCOPE, "title": "Proposer une ambiance OpenTV",
     "description": "Propose une ambiance (préréglage connu et/ou valeurs énumérées) : la TV l'affiche en aperçu et "
                    "Alex décide sur l'écran (« Non » par défaut). Tu ne peux jamais la confirmer ni la publier. "
                    "Seulement quand Alex demande de changer l'ambiance ; au plus une proposition toutes les 5 s et 20 "
                    "par 10 min ; après un refus, attends cooldown_s. Refusé pendant un réglage, un panneau ou une "
                    "lecture (tv_busy).",
     "inputSchema": tools._obj(dict({"preset": _enum(sorted(PRESETS), "Préréglage OpenTV")},
                                    **{f: _enum(ENUMS[f], _FIELD_DESC[f]) for f in FLAT_FIELDS}))},
    {"name": "canvas_cancel", "scope": SCOPE, "title": "Retirer ma proposition d'ambiance",
     "description": "Retire TA proposition d'ambiance encore en attente (identifiant renvoyé par canvas_preview). "
                    "Jamais celle d'un autre agent ; rien n'est publié.",
     "inputSchema": tools._obj({"preview_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$",
                                               "description": "Identifiant renvoyé par canvas_preview"}},
                               ("preview_id",))},
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


class Audited(tools.ToolError):
    """Refus déjà audité par l'App (outil « canvas ») : l'Agent Port ne l'enregistre pas une seconde fois."""


def public_tools(scopes):
    """Outils Canvas visibles pour ces droits (aucun sans « canvas ») ; même forme publique que tools.public_tools."""
    if SCOPE not in (scopes or ()):
        return []
    return [{k: t[k] for k in ("name", "title", "description", "inputSchema")} for t in TOOLS]


def validate_args(name, args):
    t = TOOL_BY_NAME.get(name)
    if t is None:
        raise ValueError("unknown_tool")
    out = tools.check_schema(t, args)
    if name == "canvas_preview" and not out:   # au moins un champ : jamais une proposition vide
        raise ValueError("invalid_params")
    return out


class Cooldown:
    """Délai de grâce par agent CANONIQUE après un refus/une annulation humaine (horloge monotone de l'App)."""

    def __init__(self, mono, span=REFUSAL_COOLDOWN_S):
        self._mono, self._span = mono, float(span)
        self._mu = threading.Lock()
        self._until = collections.OrderedDict()

    def note(self, agent):
        owner = agents.canonical_agent(agent)
        if not isinstance(owner, str) or owner == agents.OWNER_ACTOR:
            return
        with self._mu:
            now = self._mono()
            for k in [k for k, t in self._until.items() if t <= now]:
                del self._until[k]
            self._until.pop(owner, None)
            self._until[owner] = now + self._span
            while len(self._until) > MAX_TRACKED:
                self._until.popitem(last=False)

    def remaining(self, agent):
        with self._mu:
            left = self._until.get(agents.canonical_agent(agent), 0.0) - self._mono()
        return round(left, 1) if left > 0 else 0


def _call(fn, *args, audited=False):
    """Appel de l'App : tout CanvasError redevient un code stable, levé HORS du bloc except (aucun détail chaîné)."""
    try:
        return fn(*args)
    except CanvasError as e:
        code = CODES.get(e.code, "store_unavailable")
    raise (Audited if audited else tools.ToolError)(code)


def _flat(scene):
    return {f: scene[f] for f in FLAT_FIELDS}


def dispatch(ctx, agent, scopes, name, a):
    """Outil Canvas déjà authentifié, scope vérifié, arguments validés et débit borné par l'Agent Port."""
    if SCOPE not in scopes:   # défense en profondeur (l'Agent Port refuse déjà forbidden_scope)
        raise tools.ToolError("forbidden_scope")
    cool = getattr(ctx, "canvas_cooldown", None)
    if not isinstance(cool, Cooldown) or not callable(getattr(ctx, "canvas_agent_preview", None)):
        raise tools.ToolError("canvas_unavailable")
    if name == "canvas_status":
        st = _call(ctx.canvas_agent_status, agent)
        p = st.get("preview")
        res = {"state": st["state"], "revision": st["revision"], "preset": st["preset"], "scene": _flat(st["scene"]),
               "preview": None if p is None else {"preview_id": p["preview_id"], "preset": p["preset"],
                                                  "scene": _flat(p["scene"]), "expires_in_s": p["expires_in_s"]},
               "cooldown_s": cool.remaining(agent), "confirmation": CONFIRMATION,
               "options": dict({"preset": sorted(PRESETS)}, **{f: list(ENUMS[f]) for f in FLAT_FIELDS})}
    elif name == "canvas_preview":
        if cool.remaining(agent) > 0:
            raise tools.ToolError("canvas_cooldown")
        r = _call(ctx.canvas_agent_preview, agent, dict(a), audited=True)
        res = {"proposed": True, "preview_id": r["preview_id"], "preset": r["preset"], "scene": _flat(r["scene"]),
               "expires_in_s": r["expires_in_s"], "confirmation": CONFIRMATION}
    elif name == "canvas_cancel":
        _call(ctx.canvas_agent_cancel, agent, a["preview_id"])
        res = {"cancelled": True}
    else:
        raise tools.ToolError("unknown_tool")
    fy = getattr(ctx, "foryou", None)
    return fy.scrub(res) if fy is not None else tools._fallback_scrub(res)
