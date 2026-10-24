/* OpenTV — catalogue et apparence.
   BRANDS : habillage d'une appli, par identifiant Tizen. Les applis installées sur la TV mais
   absentes d'ici s'affichent quand même, avec leur nom réel et une tuile neutre.
   Pour connaître l'id exact d'une appli : il apparaît dans « Choisir les applis ».

   Un logo « mono » est une forme d'une seule couleur, teintée en blanc ou en couleur de marque.
   bg = dégradé de la tuile [début, fin] ; fg = couleur du logo mono ; glow = halo quand sélectionné. */
window.NOVA_BRANDS = {
  "org.tizen.netflix-app":   { name: "Netflix",      logo: "netflix",      mono: true, fg: "#E50914", bg: ["#242424", "#050505"], glow: "#E50914", tag: "Films, séries et jeux" },
  "111299001912.YouTube":    { name: "YouTube",      logo: "youtube",      mono: true, fg: "#FFFFFF", bg: ["#FF2A2A", "#D90000"], glow: "#FF0000", tag: "Des vidéos de tout le monde" },
  "org.tizen.primevideo":    { name: "Prime Video",  logo: "primevideo",   wide: true, img: "white",  bg: ["#1FA8FF", "#0A6FD6"], glow: "#1A98FF", tag: "Films, séries et sport en direct" },
  "MCmYXNxgcu.DisneyPlus":   { name: "Disney+",      logo: "disneyplus",   wide: true, img: "white",  bg: ["#1C3F9E", "#071447"], glow: "#2A9DD8", tag: "Disney, Pixar, Marvel et Star Wars" },
  "3201807016597.AppleTV":   { name: "Apple TV",     logo: "appletv",      mono: true, wide: true, fg: "#FFFFFF", bg: ["#3A3A3E", "#0B0B0C"], glow: "#DDDDDD", tag: "Apple Originals" },
  "3202301029760.HBOMax":    { name: "Max",          logo: "hbomax",       mono: true, wide: true, fg: "#FFFFFF", bg: ["#3D4BF5", "#1A1499"], glow: "#3D4BF5", tag: "HBO, films et plus" },
  "rJeHak5zRg.Spotify":      { name: "Spotify",      logo: "spotify",      mono: true, fg: "#1ED760", bg: ["#262626", "#0B0B0B"], glow: "#1DB954", tag: "Musique et balados" },
  "kIciSQlYEM.plex":         { name: "Plex",         logo: "plex",         mono: true, fg: "#EBAF00", bg: ["#2B2B2B", "#111111"], glow: "#EBAF00", tag: "Ta propre médiathèque" },
  "org.tizen.browser":       { name: "Internet",     logo: null,           icon: "web", bg: ["#33415C", "#141B2B"], glow: "#5B8DEF", tag: "Le navigateur de la TV" }
};

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
  ["clock",  "Horloge",         [["show", "Afficher"], ["hide", "Masquer"]]]
];
