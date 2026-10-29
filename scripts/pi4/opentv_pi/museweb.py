"""Pages du téléphone pour « Connecter Muse » (rendues par le serveur ; muse.js chiffre et suit l'avancement).

Trois étapes courtes, une action principale chacune :
  1. « Ton jeton Muse »  un compte Muse a UN jeton actif, valable pour plusieurs appareils (jusqu'à 50) : action
                          principale « Utiliser mon jeton existant » ; la page OFFICIELLE de Muse n'est qu'un lien
                          secondaire (pas encore de jeton). Jamais de conseil d'en créer un second ni de le révoquer
                          (révoquer déconnecterait les autres gadgets) ; si Muse n'en montre que le début masqué, il ne
                          peut pas être recopié de là : note de reprise honnête (là où il a été gardé)
  2. « Colle ton jeton »  champ masqué SANS nom (jamais envoyé par un formulaire) ; muse.js le chiffre (RSA-OAEP,
                          clé publique de la séance) avec le secret du QR, puis l'envoie en JSON ; sans JavaScript ou
                          sans aléa sûr : le champ reste caché et rien ne peut partir (échec fermé)
  3. « Dans l'app Muse »  Muse → Réglages → Appareils → Mode développeur → + → OpenTV, avec l'avancement réel
Aucune page ne contient de jeton, d'adresse technique à recopier, de commande ni de terme d'administration. Les liens
entre étapes ne portent que `s` (identifiant de séance) et `step`.
"""
import html
import urllib.parse

OFFICIAL_TOKEN_PAGE = "https://gadgets.muse.ai/settings/sdk-tokens"
STEPS = 3
PHASES = (("preparing", "Préparation"), ("waiting_muse", "En attente de Muse"), ("connecting", "Connexion"),
          ("connected", "Muse est connectée"))
PHASE_RANK = {"keygen": 0, "await_token": 0, "submitted": 1, "preparing": 1, "waiting_muse": 2, "connecting": 3,
              "connected": 4}
PAIR_PATH = "Muse → Réglages → Appareils → Mode développeur → + → OpenTV"


def page(title, inner, data=None):
    attrs = "".join(" data-%s=\"%s\"" % (k, html.escape(str(v), quote=True)) for k, v in sorted((data or {}).items())
                    if v is not None)
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><meta name=\"color-scheme\" content=\"dark\">"
            "<title>%s</title><link rel=\"stylesheet\" href=\"/static/setup.css\">"
            "<script src=\"/static/muse.js\" defer></script></head>"
            "<body><main class=\"muse\" id=\"muse\"%s>%s</main></body></html>" % (html.escape(title), attrs, inner))


def progress(n):
    dots = "".join("<span class=\"dot%s\"></span>" % (" on" if i <= n else "") for i in range(1, STEPS + 1))
    return ("<p class=\"steps\"><span class=\"dots\" aria-hidden=\"true\">%s</span>Étape %d sur %d</p>"
            % (dots, n, STEPS))


def step_url(sid, step):
    return html.escape("/muse?" + urllib.parse.urlencode([("s", sid), ("step", str(step))]), quote=True)


CANCEL = ("<button type=\"button\" class=\"secondary\" id=\"mcancel\" hidden>Annuler, rien ne change</button>")


def step1(sid, view):
    return page("Connecter Muse", progress(1) + (
        "<h1>Ton jeton Muse</h1>"
        "<p class=\"lead\" id=\"multi\">Ton compte Muse a un seul jeton, valable pour plusieurs appareils (jusqu'à "
        "50). Utilise le même que pour tes autres gadgets\u00a0: inutile d'en créer un autre.</p>"
        "<a class=\"primary\" id=\"next\" href=\"%s\">Utiliser mon jeton existant</a>"
        "<p class=\"warn\" id=\"keeptoken\">Ne révoque pas ce jeton\u00a0: tes autres gadgets Muse seraient "
        "déconnectés.</p>"
        "<p class=\"hint\" id=\"masked\">La page Muse n'en montre que le début, masqué\u00a0: il ne peut pas être "
        "recopié de là. Reprends-le où tu l'avais gardé (gestionnaire de mots de passe, notes). Si tu ne le retrouves "
        "pas, demande de l'aide plutôt que de le changer.</p>"
        "<p class=\"more\">Pas encore de jeton\u00a0? <a id=\"official\" href=\"%s\" target=\"_blank\" "
        "rel=\"noopener noreferrer\">Ouvrir la page Muse</a></p>"
        "<p class=\"hint\">Ton compte et ta connexion restent chez Muse. OpenTV n'affiche jamais ton jeton.</p>%s"
        % (step_url(sid, 2), html.escape(OFFICIAL_TOKEN_PAGE, quote=True), CANCEL)),
        {"s": sid, "c": view["csrf"], "step": 1})


def step2(sid, view):
    jwk = view.get("jwk") or {}
    return page("Connecter Muse", progress(2) + (
        "<h1>Colle ton jeton</h1>"
        "<p class=\"lead\">Le jeton complet, le même que pour tes autres appareils Muse.</p>"
        "<noscript><p class=\"err\" role=\"alert\">Ce navigateur a JavaScript désactivé : le jeton ne peut pas être "
        "protégé, il n'est donc pas demandé ici. Rien n'a été envoyé. Active JavaScript, puis rescanne le QR de la "
        "TV.</p></noscript>"
        "<p class=\"err\" id=\"nosecure\" role=\"alert\" hidden>Ce navigateur ne peut pas protéger le jeton : rien "
        "n'a été envoyé. Rescanne le QR de la TV avec l'appareil photo du téléphone (Safari ou Chrome à jour).</p>"
        "<form id=\"seal\" method=\"post\" action=\"/muse/nojs\" autocomplete=\"off\" hidden>"
        "<label for=\"tok\">Jeton Muse</label>"
        "<input id=\"tok\" type=\"password\" autocomplete=\"off\" autocapitalize=\"none\" autocorrect=\"off\" "
        "spellcheck=\"false\" inputmode=\"text\" enterkeyhint=\"send\" maxlength=\"200\" "
        "data-1p-ignore=\"true\" data-lpignore=\"true\" aria-describedby=\"tokhint\">"
        "<p class=\"err\" id=\"merr\" role=\"alert\" hidden></p>"
        "<button type=\"submit\" class=\"primary\" id=\"send\">Envoyer à OpenTV</button></form>"
        "<p class=\"hint\" id=\"tokhint\">Le jeton part chiffré, seulement vers ta boîte OpenTV. Il n'est jamais "
        "affiché, ni gardé sur ce téléphone.</p>"
        "<p class=\"more\"><a id=\"back\" href=\"%s\">← Retour</a></p>%s" % (step_url(sid, 1), CANCEL)),
        {"s": sid, "c": view["csrf"], "step": 2, "n": jwk.get("n"), "e": jwk.get("e")})


def _phases_html(state):
    r = PHASE_RANK.get(state, -1)
    out = []
    for i, (k, label) in enumerate(PHASES, start=1):
        cls = "done" if r > i or state == "connected" else "on" if r == i else ""
        out.append("<li class=\"%s\" data-phase=\"%s\">%s</li>" % (cls, k, html.escape(label)))
    return "<ol class=\"mphases\" id=\"mphases\">%s</ol>" % "".join(out)


def step3(sid, view):
    st = view["state"]
    final = st in ("connected", "failed", "cancelled", "expired")
    if st == "connected":
        status = "Muse est connectée ! Tu peux fermer cette page."
    elif final:
        status = view.get("message") or "La connexion n'a pas abouti. Recommence depuis la TV."
    else:
        status = view["label"] + "…"
    ble = view.get("ble_name")
    return page("Connecter Muse", progress(3) + (
        "<h1>Termine dans l'app Muse</h1>"
        "<ol class=\"howto\"><li>Ouvre l'app <strong>Muse</strong> sur ton iPhone.</li>"
        "<li><strong>Réglages → Appareils → Mode développeur</strong></li>"
        "<li>Touche <strong>+</strong>, puis choisis <strong>OpenTV</strong>%s.</li></ol>"
        "<p class=\"lead mstatus%s\" id=\"mstatus\" role=\"status\" aria-live=\"polite\">%s</p>%s"
        "<p class=\"hint\">Garde l'iPhone près de la TV. L'appairage se fait dans l'app Muse ; OpenTV n'affiche "
        "jamais ton jeton.</p>"
        "<noscript><p class=\"more\"><a href=\"%s\">Actualiser l'avancement</a></p></noscript>%s"
        % (" (nom affiché\u00a0: <span id=\"mble\">%s</span>)" % html.escape(ble) if ble else
           "<span id=\"mble\"></span>", " fail" if final and st != "connected" else "", html.escape(status),
           _phases_html(st), step_url(sid, 3), "" if final else CANCEL)),
        {"s": sid, "c": view["csrf"], "step": 3, "state": st})


def render(sid, view, step):
    """Étape demandée, bornée par l'état réel : avant le jeton 1–2, après le jeton toujours 3 (retour/rechargement
    sûrs : un jeton déjà envoyé n'est jamais redemandé)."""
    if view["state"] in ("keygen", "await_token"):
        step = 2 if step == 2 else 1
        if step == 2 and not view.get("jwk"):
            step = 1
        return step2(sid, view) if step == 2 else step1(sid, view)
    return step3(sid, view)


def denied_page():
    return page("Connecter Muse", "<h1>QR expiré ou déjà utilisé</h1><p class=\"lead\">Sur la TV : Réglages → Gérer "
                                  "les assistants → Muse, puis scanne le nouveau QR.</p>")


def nojs_page():
    return page("Connecter Muse", "<h1>Rien n'a été envoyé</h1><p class=\"lead\">Le jeton ne part que chiffré, avec "
                                  "JavaScript actif. Active JavaScript puis rescanne le QR de la TV.</p>")
