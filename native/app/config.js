/* OpenTV — catalogue et apparence.
   BRANDS : habillage d'une appli. Le reconnaissance se fait par le NOM de l'appli (champ "match",
   en minuscules) — ou par identifiant exact (champ "ids") si besoin. Comme ça les logos se collent
   quelle que soit la TV, sans connaître l'identifiant interne. Les applis non reconnues s'affichent
   avec leur nom réel et une tuile neutre.

   Un logo "mono" est une forme d'une seule couleur, teintée en blanc ou en couleur de marque.
   bg = dégradé de la tuile [début, fin] ; fg = couleur du logo mono ; glow = halo quand sélectionné. */
window.NOVA_BRANDS = [
  { key: "netflix",    name: "Netflix",      match: ["netflix"],                      logo: "netflix",    mono: true, fg: "#E50914", bg: ["#242424", "#050505"], glow: "#E50914", tag: "Films, séries et jeux" },
  { key: "youtube",    name: "YouTube",      match: ["youtubetv", "youtube tv", "youtube"], logo: "youtube", mono: true, fg: "#FFFFFF", bg: ["#FF2A2A", "#D90000"], glow: "#FF0000", tag: "Des vidéos de tout le monde" },
  { key: "primevideo", name: "Prime Video",  match: ["prime video", "primevideo", "prime"], logo: "primevideo", wide: true, img: "white", bg: ["#1FA8FF", "#0A6FD6"], glow: "#1A98FF", tag: "Films, séries et sport en direct" },
  { key: "disney",     name: "Disney+",      match: ["disney"],                       logo: "disneyplus", wide: true, img: "white", bg: ["#1C3F9E", "#071447"], glow: "#2A9DD8", tag: "Disney, Pixar, Marvel et Star Wars" },
  { key: "appletv",    name: "Apple TV",     match: ["apple tv", "appletv"],          logo: "appletv",    mono: true, wide: true, fg: "#FFFFFF", bg: ["#3A3A3E", "#0B0B0C"], glow: "#DDDDDD", tag: "Apple Originals" },
  { key: "hbomax",     name: "Max",          match: ["hbo max", "hbomax", "hbo"],     logo: "hbomax",     mono: true, wide: true, fg: "#FFFFFF", bg: ["#3D4BF5", "#1A1499"], glow: "#3D4BF5", tag: "HBO, films et plus" },
  { key: "spotify",    name: "Spotify",      match: ["spotify"],                      logo: "spotify",    mono: true, fg: "#1ED760", bg: ["#262626", "#0B0B0B"], glow: "#1DB954", tag: "Musique et balados" },
  { key: "plex",       name: "Plex",         match: ["plex"],                         logo: "plex",       mono: true, fg: "#EBAF00", bg: ["#2B2B2B", "#111111"], glow: "#EBAF00", tag: "Ta propre médiathèque" },
  { key: "tubi",       name: "Tubi",         match: ["tubi"],                         logo: "tubi",       mono: true, wide: true, fg: "#7408FF", bg: ["#8A2BFF", "#5200CC"], glow: "#7408FF", tag: "Films et séries gratuits" },
  { key: "crunchyroll",name: "Crunchyroll",  match: ["crunchyroll"],                  logo: "crunchyroll",mono: true, fg: "#FF5E00", bg: ["#FF7A26", "#E84E00"], glow: "#FF5E00", tag: "Anime" },
  { key: "paramount",  name: "Paramount+",   match: ["paramount"],                    logo: "paramountplus", mono: true, fg: "#0064FF", bg: ["#1A7BFF", "#0040B8"], glow: "#0064FF", tag: "Films et séries" },
  { key: "samsungtvplus", name: "Samsung TV Plus", match: ["tv plus", "samsung tv plus"], logo: null, icon: "tv", bg: ["#5B5BF0", "#1A1A8A"], glow: "#6B6BFF", tag: "Chaînes gratuites" },
  { key: "livetv",     name: "Live TV",      match: ["live tv", "livetv", "channel"], logo: null, icon: "tv", bg: ["#2B6CB0", "#143A6B"], glow: "#3B8DEF", tag: "Télé en direct" },
  { key: "browser",    name: "Internet",     match: ["internet", "browser"],          logo: null, icon: "web", bg: ["#33415C", "#141B2B"], glow: "#5B8DEF", tag: "Le navigateur de la TV" },
  { key: "appletv2",   name: "Apple Music",  match: ["apple music"],                  logo: "applemusic", mono: true, fg: "#FA243C", bg: ["#FF6B81", "#F0243C"], glow: "#FA243C", tag: "Musique" }
];

/* Applis système Samsung, pour les raccourcis en haut à droite. */
window.NOVA_ACTIONS = [
  { label: "Entrées",     icon: "input",    app: "org.tizen.SMSourceAPP" },
  { label: "Paramètres",  icon: "settings", app: "com.samsung.tv.coba.setting" }
];

/* Fonds photo livrés avec Nova. */
window.NOVA_PRESETS = [
  ["misty-coast", "Côte brumeuse"], ["quiet-cove", "Crique paisible"], ["forest-mist", "Forêt dans la brume"],
  ["mountain-valley", "Vallée de montagne"], ["desert-dunes", "Dunes du désert"], ["blue-peaks", "Sommets bleus"],
  ["city-dusk", "Ville au crépuscule"], ["golden-hour", "Heure dorée"]
];

/* Un design = un look complet en un choix. Chaque partie reste modifiable dans Apparence. */
window.NOVA_DESIGNS = [
  { id: "shore",   name: "Rivage",   desc: "Tuiles de verre sur une photo",
    p: { layout: "grid", tiles: "glass", labels: "both", logo: "white", font: "modern", clock: "show", bg: { type: "preset", id: "misty-coast" } } },
  { id: "icons",   name: "Icônes",   desc: "Icônes colorées, façon Apple TV",
    p: { layout: "grid", tiles: "brand", labels: "both", logo: "brand", font: "modern", clock: "show", bg: { type: "preset", id: "mountain-valley" } } },
  { id: "minimal", name: "Minimal",  desc: "Juste l'heure et tes logos",
    p: { layout: "grid", tiles: "clear", labels: "logo", logo: "white", font: "modern", clock: "show", bg: { type: "preset", id: "forest-mist" } } }
];

/* Réglages d'apparence modifiables un par un. */
window.NOVA_LOOK = [
  ["tiles",  "Tuiles",          [["glass", "Verre"], ["brand", "Couleurs de marque"], ["frost", "Givré"], ["dark", "Verre foncé"], ["clear", "Transparent"]]],
  ["labels", "Libellés",        [["both", "Logo et nom"], ["logo", "Logo seul"], ["name", "Nom seul"]]],
  ["logo",   "Couleur du logo", [["white", "Blanc"], ["brand", "Couleurs de marque"]]],
  ["font",   "Police",          [["modern", "Moderne"], ["elegant", "Élégante"], ["rounded", "Arrondie"], ["grotesk", "Grotesk"]]],
  ["clock",  "Horloge",         [["show", "Afficher"], ["hide", "Masquer"]]],
  ["saver",  "Veille",          [["hdmi", "HDMI 1 en direct"], ["clock", "Horloge"]]]
];

/* Applis EXTERNES rendues par le NanoPi sur HDMI 1 (contrat : native/NANOPI-API.md).
   Ce ne sont PAS des applis Tizen : OpenTV n'envoie que la clé et des touches bornées, jamais d'URL. */
window.NOVA_NANOPI = window.NOVA_NANOPI || { base: "http://192.168.2.28:8097" };
window.NOVA_EXTERNAL = window.NOVA_EXTERNAL || [
  { key: "catalogue", name: "Films", icon: "film", bg: ["#3A2A5C", "#140E24"], glow: "#9B6BFF" },
  /* « Pour toi » : même catalogue, sélection personnelle et actions (J'aime, Déjà vu…) gérées par le NanoPi. */
  { key: "pourtoi", name: "Pour toi", icon: "star", bg: ["#5C2A4A", "#240E1C"], glow: "#FF6BB5" }
];
