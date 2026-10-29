"""Outils typés de l'Agent Port (source unique des schémas MCP et de la validation côté service)."""
import re

from . import validate

FEEDBACK_ACTIONS = ("clear-not-tonight", "clear-opinion", "dislike", "like", "not-tonight", "seen", "unseen",
                    "unwatchlist", "watchlist")   # = opentv_foryou.ACTIONS (vérifié par les tests)
HISTORY_LISTS = ("events", "opened", "liked", "disliked", "seen", "watchlist", "not-tonight")
UID_PATTERN = r"^[a-z][a-z0-9-]{1,23}/[0-9]{1,12}$"
NAV_KEYS = ("up", "down", "left", "right", "ok", "back", "home")   # = REMOTE_ACTIONS de tv.js (vérifié par les tests)


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
     "description": "Sélection personnelle expliquée (goûts explicites, notes, nouveautés), filtrée sur le catalogue "
                    "accepté (paginée). Point de départ pour choisir TOI-MÊME un film à proposer à Alex.",
     "inputSchema": _obj({"limit": _int(1, 20, 5, "Nombre d'éléments"), "offset": _int(0, 200, 0, "Décalage")})},
    {"name": "catalogue_list", "scope": "read", "title": "Catalogue OpenTV complet",
     "description": "Liste alphabétique paginée de tout le catalogue accepté, sans URL ni champ interne. Parcourir "
                    "avec offset jusqu'à has_more=false en gardant generated_at : s'il change, recommencer à 0.",
     "inputSchema": _obj({"limit": _int(1, 50, 20, "Nombre d'éléments"),
                          "offset": _int(0, 2000, 0, "Décalage")})},
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
    # ---- pilotage (scope « control », accordé explicitement par le propriétaire) : commandes SÉMANTIQUES typées,
    # jamais d'URL, de chemin, de touche brute de la TV, de durée libre ni de boucle ; résultats = état accepté/persisté
    # côté service (file d'actions TV numérotée, état de lecture), jamais une affirmation sur les pixels.
    {"name": "present_title", "scope": "control", "title": "Proposer un film à l'écran",
     "description": "Montre sur la TV la fiche d'un film du catalogue accepté (identifiant stable de recommend, search "
                    "ou catalogue_list), avec une courte phrase d'explication facultative. Choisis toi-même le film "
                    "(goûts « Pour toi », historique, notes), explique ton choix en une phrase, puis appelle cet outil : "
                    "Alex décide avec sa télécommande ou en te demandant de le lancer. Refusé pendant une lecture ou "
                    "un réglage en cours. Résultat : action mise en file (queued) ; suivre son état avec status.",
     "inputSchema": _obj({"id": {"type": "string", "pattern": UID_PATTERN, "description": "ex. films/17397"},
                          "text": {"type": "string", "minLength": 1, "maxLength": 140,
                                   "description": "Pourquoi ce film (une phrase, sans adresse)"}}, ("id",))},
    {"name": "play_title", "scope": "control", "title": "Lancer un film (à la demande d'Alex)",
     "description": "Lance un film du catalogue accepté par le lecteur officiel promu d'OpenTV, SEULEMENT quand Alex "
                    "demande explicitement de le regarder. Mêmes portes que le bouton « Regarder » (source approuvée, "
                    "promotion terrain, contrôleur) ; refusé si indisponible ou si une lecture est déjà en cours. "
                    "Résultat : lecture acceptée (loading), jamais « visible » ; suivre playback dans status.",
     "inputSchema": _obj({"id": {"type": "string", "pattern": UID_PATTERN, "description": "ex. films/17397"}},
                         ("id",))},
    {"name": "close_player", "scope": "control", "title": "Fermer le lecteur",
     "description": "Ferme le lecteur en cours et revient à OpenTV sur la fiche du film. Sans lecture en cours : "
                    "rien n'est fermé (closed=false).",
     "inputSchema": _obj({})},
    {"name": "remote_control", "scope": "control", "title": "Touche de navigation OpenTV",
     "description": "Une touche de navigation dans OpenTV (haut, bas, gauche, droite, OK, retour, accueil). Préfère "
                    "present_title/play_title/close_player ; débit borné, la télécommande d'Alex reste prioritaire, "
                    "jamais d'accès aux Réglages ni de lancement de film par OK (utiliser play_title). Pendant une "
                    "lecture, seules back/home sont acceptées (elles ferment le lecteur).",
     "inputSchema": _obj({"key": {"type": "string", "enum": list(NAV_KEYS)}}, ("key",))},
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}
CONTROL_TOOLS = tuple(t["name"] for t in TOOLS if t["scope"] == "control")


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
    if name == "display_message" or (name == "present_title" and "text" in out):
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
    elif name == "catalogue_list":
        fy = getattr(ctx, "foryou", None)
        if fy is None:
            raise ToolError("foryou_unavailable")
        res = fy.catalogue_page(a["limit"], a["offset"])
    elif name == "search":
        # « --query=<texte> » (une seule option) : une requête qui commence par « - » (« -x », « --limit ») reste un
        # titre littéral, jamais une option ni une erreur « usage » de l'analyseur du moteur
        res = _cli(ctx, ["search", "--query=" + a["query"], "--limit", a["limit"], "--offset", a["offset"]])
    elif name == "feedback":
        res = _cli(ctx, ["feedback", "--id", a["id"], "--action", a["action"]])
    elif name == "history":
        res = _cli(ctx, ["history", "--list", a["list"], "--limit", a["limit"], "--offset", a["offset"]])
    elif name == "display_message":
        res = ctx.show_message(agent, a["text"], a["seconds"])
    elif name in CONTROL_TOOLS:
        # pilotage : le contexte (App) est seul juge (catalogue accepté, portes de lecture, priorité humaine) ;
        # refus = ToolError(code stable), jamais une exception interne vers l'agent
        fn = getattr(ctx, "agent_control", None)
        if not callable(fn):
            raise ToolError("control_unavailable")
        res = fn(agent, name, a)
    else:
        raise ToolError("unknown_tool")
    fy = getattr(ctx, "foryou", None)
    return fy.scrub(res) if fy is not None else _fallback_scrub(res)
