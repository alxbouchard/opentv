"""Outils typés de l'Agent Port (source unique des schémas MCP et de la validation côté service)."""
import re

from . import validate

FEEDBACK_ACTIONS = ("clear-not-tonight", "clear-opinion", "dislike", "like", "not-tonight", "seen", "unseen",
                    "unwatchlist", "watchlist")   # = opentv_foryou.ACTIONS (vérifié par les tests)
HISTORY_LISTS = ("events", "opened", "liked", "disliked", "seen", "watchlist", "not-tonight")
UID_PATTERN = r"^[a-z][a-z0-9-]{1,23}/[0-9]{1,12}$"


def _int(lo, hi, default, desc):
    return {"type": "integer", "minimum": lo, "maximum": hi, "default": default, "description": desc}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "status", "scope": "read", "title": "État d'OpenTV",
     "description": "État de la boîte OpenTV : nom, configuration, réseau (mode seulement), affichage, CEC "
                    "(découverte), catalogue et profil « Pour toi ». Aucune adresse ni secret.",
     "inputSchema": _obj({})},
    {"name": "recommend", "scope": "read", "title": "Recommandations « Pour toi »",
     "description": "Sélection personnelle expliquée, filtrée sur le catalogue accepté (paginée).",
     "inputSchema": _obj({"limit": _int(1, 20, 5, "Nombre d'éléments"), "offset": _int(0, 200, 0, "Décalage")})},
    {"name": "search", "scope": "read", "title": "Chercher un titre",
     "description": "Recherche par titre dans le catalogue accepté. Une requête qui ressemble à une URL est refusée.",
     "inputSchema": _obj({"query": {"type": "string", "minLength": 1, "maxLength": 80, "description": "Titre"},
                          "limit": _int(1, 20, 5, "Nombre d'éléments"), "offset": _int(0, 200, 0, "Décalage")},
                         ("query",))},
    {"name": "feedback", "scope": "feedback", "title": "Avis sur un titre",
     "description": "Enregistre un avis explicite (J'aime, Pas pour moi, Déjà vu, À voir plus tard, Pas ce soir) "
                    "sur un identifiant du catalogue. Idempotent. Ouvrir un film n'est jamais « vu ».",
     "inputSchema": _obj({"id": {"type": "string", "pattern": UID_PATTERN, "description": "ex. films/17397"},
                          "action": {"type": "string", "enum": list(FEEDBACK_ACTIONS)}}, ("id", "action"))},
    {"name": "history", "scope": "read", "title": "Historique « Pour toi »",
     "description": "Historique local (changements, ouvertures, listes) paginé.",
     "inputSchema": _obj({"list": {"type": "string", "enum": list(HISTORY_LISTS), "default": "events"},
                          "limit": _int(1, 50, 20, "Nombre d'éléments"), "offset": _int(0, 500, 0, "Décalage")})},
    {"name": "display_message", "scope": "display", "title": "Afficher un message sur la TV",
     "description": "Affiche une courte bannière (texte seul, sans adresse) sur l'écran OpenTV. Limité à un "
                    "message toutes les 5 secondes par agent.",
     "inputSchema": _obj({"text": {"type": "string", "minLength": 1, "maxLength": 140},
                          "seconds": _int(5, 60, 15, "Durée d'affichage")}, ("text",))},
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools(scopes):
    return [{k: t[k] for k in ("name", "title", "description", "inputSchema")} for t in TOOLS if t["scope"] in scopes]


def validate_args(name, args):
    """Validation stricte selon le schéma (types, bornes, enum, motif, champs inconnus). -> args complétés."""
    t = TOOL_BY_NAME.get(name)
    if t is None:
        raise ValueError("unknown_tool")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("invalid_params")
    sch = t["inputSchema"]
    props = sch["properties"]
    if set(args) - set(props):
        raise ValueError("invalid_params")
    for r in sch["required"]:
        if r not in args:
            raise ValueError("invalid_params")
    out = {}
    for k, p in props.items():
        if k not in args:
            if "default" in p:
                out[k] = p["default"]
            continue
        v = args[k]
        if p["type"] == "integer":
            if not isinstance(v, int) or isinstance(v, bool) or not p["minimum"] <= v <= p["maximum"]:
                raise ValueError("invalid_params")
        elif p["type"] == "string":
            if not isinstance(v, str) or not p.get("minLength", 0) <= len(v) <= p.get("maxLength", 200) or \
                    any(ord(c) < 32 or ord(c) == 127 for c in v):
                raise ValueError("invalid_params")
            if "enum" in p and v not in p["enum"]:
                raise ValueError("invalid_params")
            if "pattern" in p and not re.match(p["pattern"], v):
                raise ValueError("invalid_params")
        out[k] = v
    if name == "display_message":
        out["text"] = validate.message_text(out["text"])
    return out


class ToolError(Exception):
    pass


def _fallback_scrub(obj):
    """Sans moteur (installation incomplète) : toute chaîne ressemblant à une adresse est retirée."""
    if isinstance(obj, str):
        return obj if validate.no_url(obj) else ""
    if isinstance(obj, dict):
        return {k: _fallback_scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_fallback_scrub(v) for v in obj]
    return obj


def _cli(ctx, argv):
    if getattr(ctx, "foryou", None) is None:
        raise ToolError("foryou_unavailable")
    code, out = ctx.foryou.cli(argv)
    if code != 0 or not out.get("ok"):
        raise ToolError(out.get("error") if isinstance(out.get("error"), str) else "failed")
    out.pop("ok", None)
    return out


def dispatch(ctx, agent, scopes, name, a):
    """Exécute un outil déjà validé. Toute sortie passe par le nettoyage global du moteur (aucune URL)."""
    if name == "status":
        res = ctx.agent_status(agent, scopes)
        try:
            res["foryou"] = _cli(ctx, ["status"])
        except ToolError as e:
            res["foryou"] = {"error": str(e)}
    elif name == "recommend":
        res = _cli(ctx, ["recommend", "--limit", a["limit"], "--offset", a["offset"]])
    elif name == "search":
        res = _cli(ctx, ["search", "--query", a["query"], "--limit", a["limit"], "--offset", a["offset"]])
    elif name == "feedback":
        res = _cli(ctx, ["feedback", "--id", a["id"], "--action", a["action"]])
    elif name == "history":
        res = _cli(ctx, ["history", "--list", a["list"], "--limit", a["limit"], "--offset", a["offset"]])
    elif name == "display_message":
        res = ctx.show_message(agent, a["text"], a["seconds"])
    else:
        raise ToolError("unknown_tool")
    fy = getattr(ctx, "foryou", None)
    return fy.scrub(res) if fy is not None else _fallback_scrub(res)
