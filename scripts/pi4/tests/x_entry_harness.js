/* Harnais déterministe de la saisie de l'identifiant X par téléphone (tests seulement, jamais installé).
   argv[2] = dossier scripts/pi4 ; argv[3] = xphone.js à tester (défaut ui/xphone.js) ; argv[4] = xentry.js à tester
   (défaut x-tv-extension/xentry.js) — les tests de mutation passent des copies altérées.
   A. Téléphone : VRAIS ui/muse.js + ui/xphone.js dans un contexte vm (window, sans document) : fragment, vérification de
      la clé (empreinte du QR, e = 65537, module de 2048 bits), refus (vide, code, trop long), nettoyage, chiffré de
      taille FIXE, aléatoire, déchiffré par WebCrypto (clé NON exportable, comme l'extension) et par OpenSSL.
   B. Extension : bloc <gate> … </gate> de x-tv-extension/xentry.js exécuté TEL QUEL : URL de connexion, faits du champ
      (mot de passe, OTP, code, NIP, 2FA, passkey, secours, paiement EN/FR jamais éligibles), document déchiffré (code
      de 4 à 9 chiffres refusé par l'extension elle-même), décision finale de remplissage (fillOk).
   C. Page du téléphone : VRAI xphone.js sur un DOM factice, réponses du service dans l'ordre choisi (courses Envoyer /
      Annuler) : Annuler caché + désactivé dès l'envoi accepté, textes tirés de la réponse du service, issue gardée.
   Sortie : une ligne JSON {ok, failures, counts} ; code de sortie 1 si une attente échoue. */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const nodeCrypto = require("crypto");

const PI4 = process.argv[2];
const XPHONE = process.argv[3] || path.join(PI4, "ui", "xphone.js");
const XENTRY = process.argv[4] || path.join(PI4, "x-tv-extension", "xentry.js");
const failures = [];
const counts = { phone: 0, gate: 0, ident: 0, fill: 0, ui: 0, place: 0 };
function expect(group, name, got, want) {
  counts[group] += 1;
  if (JSON.stringify(got) !== JSON.stringify(want)) { failures.push({ group, name, got, want }); }
}
const b64u = (buf) => Buffer.from(buf).toString("base64url");

async function phone() {
  const sb = { btoa: (s) => Buffer.from(s, "latin1").toString("base64"),
               atob: (s) => Buffer.from(s, "base64").toString("latin1"), crypto: nodeCrypto.webcrypto };
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(PI4, "ui", "muse.js"), "utf8"), sb, { filename: "muse.js" });
  vm.runInContext(fs.readFileSync(XPHONE, "utf8"), sb, { filename: "xphone.js" });
  const seal = sb.window.OpenTVMuseSeal;
  const X = sb.window.OpenTVXPhone;
  const rnd = (n) => Array.from(nodeCrypto.randomBytes(n));
  // clé de l'extension : WebCrypto, privée NON exportable (mêmes paramètres que xentry.js)
  const subtle = nodeCrypto.webcrypto.subtle;
  const pair = await subtle.generateKey({ name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]),
                                          hash: "SHA-256" }, false, ["encrypt", "decrypt"]);
  expect("phone", "private_not_extractable", pair.privateKey.extractable, false);
  const jwk = await subtle.exportKey("jwk", pair.publicKey);
  const fp = b64u(nodeCrypto.createHash("sha256").update(Buffer.from(jwk.n, "base64url")).digest());
  const other = nodeCrypto.generateKeyPairSync("rsa", { modulusLength: 2048 }).publicKey.export({ format: "jwk" });
  const small = nodeCrypto.generateKeyPairSync("rsa", { modulusLength: 1024 }).publicKey.export({ format: "jwk" });
  const smallFp = b64u(nodeCrypto.createHash("sha256").update(Buffer.from(small.n, "base64url")).digest());
  const e3 = nodeCrypto.generateKeyPairSync("rsa", { modulusLength: 2048, publicExponent: 3 }).publicKey.export({ format: "jwk" });
  const e3Fp = b64u(nodeCrypto.createHash("sha256").update(Buffer.from(e3.n, "base64url")).digest());
  expect("phone", "key_good", X.verifyKey(seal, jwk.n, jwk.e, fp), true);
  expect("phone", "key_other_fp", X.verifyKey(seal, other.n, other.e, fp), false);
  expect("phone", "key_fp_flip", X.verifyKey(seal, jwk.n, jwk.e, (fp[0] === "A" ? "B" : "A") + fp.slice(1)), false);
  expect("phone", "key_e3", X.verifyKey(seal, e3.n, e3.e, e3Fp), false);
  expect("phone", "key_e1", X.verifyKey(seal, jwk.n, "AQ", fp), false);
  expect("phone", "key_1024", X.verifyKey(seal, small.n, small.e, smallFp), false);
  expect("phone", "key_not_string", X.verifyKey(seal, null, "AQAB", fp), false);
  // module de 256 octets mais bit fort à 0 (2047 bits au plus) : jamais RSA-2048, même avec l'empreinte juste
  const low = Buffer.from(jwk.n, "base64url");
  low[0] = 0x40;
  const lowFp = b64u(nodeCrypto.createHash("sha256").update(low).digest());
  expect("phone", "key_top_bit_clear", X.verifyKey(seal, b64u(low), "AQAB", lowFp), false);
  // base64url non canonique (bits de bourrage non nuls) : refusé
  const nc = jwk.n.slice(0, -1) + (jwk.n.slice(-1) === "A" ? "B" : "A");
  const ncFp = b64u(nodeCrypto.createHash("sha256").update(Buffer.from(nc, "base64url")).digest());
  expect("phone", "key_noncanonical", X.verifyKey(seal, nc, "AQAB", ncFp), Buffer.from(nc, "base64url").toString("base64url") === nc);
  const sid = "S".repeat(22);
  const f = "F".repeat(22);
  expect("phone", "frag_good", X.parseFragment(f + "." + fp), { f: f, fp: fp });
  for (const bad of ["", f, f + "." + fp + "x", f + fp, "<" + f.slice(1) + "." + fp, f + "." + fp.slice(1), null]) {
    expect("phone", "frag_bad:" + bad, X.parseFragment(bad), null);
  }
  expect("phone", "refuse_empty", X.refusal(""), "empty");
  expect("phone", "refuse_code", X.refusal("123456"), "code");
  expect("phone", "refuse_code_spaced", X.refusal("123 456"), "code");
  expect("phone", "refuse_long", X.refusal("a".repeat(101)), "long");
  for (const ok of ["alex", "alex@example.com", "+1 514 555 1234", "a".repeat(100), "5145551234"]) {
    expect("phone", "accept:" + ok.slice(0, 12), X.refusal(ok), "");
  }
  expect("phone", "clean", X.clean("  al\u202eex\u200b\n@x.com\u0007 "), "alex@x.com");
  // chiffré : taille FIXE (342 caractères = 256 octets) quelle que soit la longueur, aléatoire, lisible par la clé
  const sizes = new Set();
  const cts = [];
  for (const id of ["a", "alex@example.com", "x".repeat(100)]) {
    const ct = X.makeCt(seal, jwk.n, sid, f, id, rnd);
    sizes.add(ct.length);
    cts.push(ct);
    const pt = new Uint8Array(await subtle.decrypt({ name: "RSA-OAEP" }, pair.privateKey, Buffer.from(ct, "base64url")));
    expect("phone", "roundtrip:" + id.length, JSON.parse(Buffer.from(pt).toString("utf8")), { v: 1, s: sid, f: f, t: id });
  }
  expect("phone", "fixed_size", Array.from(sizes), [342]);
  expect("phone", "randomized", X.makeCt(seal, jwk.n, sid, f, "a", rnd) !== cts[0], true);
  let tooBig = false;
  try { X.makeCt(seal, jwk.n, sid, f, "é".repeat(100), rnd); } catch (err) { tooBig = true; }
  expect("phone", "doc_over_oaep_limit_refused", tooBig, true);
  // OpenSSL (autre implémentation) relit aussi le chiffré de muse.js : OAEP SHA-256 / MGF1-SHA-256 exact
  const kp = nodeCrypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const kj = kp.publicKey.export({ format: "jwk" });
  const ct2 = X.makeCt(seal, kj.n, sid, f, "bob", rnd);
  const pt2 = nodeCrypto.privateDecrypt({ key: kp.privateKey, padding: nodeCrypto.constants.RSA_PKCS1_OAEP_PADDING,
                                          oaepHash: "sha256" }, Buffer.from(ct2, "base64url"));
  expect("phone", "openssl_roundtrip", JSON.parse(pt2.toString("utf8")).t, "bob");
}

function gate() {
  const src = fs.readFileSync(XENTRY, "utf8");
  const a = src.indexOf("// <gate>");
  const b = src.indexOf("// </gate>");
  if (a < 0 || b < a) { failures.push({ group: "gate", name: "markers" }); return; }
  const G = new Function(src.slice(a, b) + "\nreturn { loginUrl, factsOk, identOf, fillOk, ID_MAX };")();
  const base = { top: true, focused: true, visible: true, tag: "INPUT", disabled: false, readonly: false,
                 url: "https://x.com/i/flow/login", type: "text", ac: "username", inputmode: "", maxlength: -1,
                 pattern: "", danger: false, texts: ["text", "", "", "", "", "", "Phone, email, or username",
                                                      "Sign in to X"] };
  const v = (o) => G.factsOk(Object.assign({}, base, o));
  expect("gate", "base", v({}), true);
  const ONB = "/i/jf/onboarding/web";
  const LANDING = { url: "https://x.com/", id: "landing-identifier", name: "username", formAction: ONB,
                    formModes: [{ type: "hidden", valueAttr: "login" }],
                    texts: ["username", "landing-identifier", " ", "", "", "", "Email or usernameEmail or username"] };
  const yes = { webauthn: { ac: "username webauthn" }, email: { ac: "email", type: "email" }, tel: { ac: "tel", type: "tel" },
                tel_national: { ac: "tel-national", type: "tel" }, tel_local: { ac: "TEL-LOCAL", type: "tel" },
                onboarding: { url: "https://x.com/i/jf/onboarding/web?mode=login&redirect_after_login=%2F" },
                twitter: { url: "https://twitter.com/login" }, mobile: { url: "https://mobile.x.com/i/flow/login" },
                www: { url: "https://www.x.com/i/flow/login/" }, maxlen_long: { maxlength: 512 },
                fr_label: { texts: ["Téléphone, adresse e-mail ou nom d'utilisateur", "Connexion à X"] },
                // accueil déconnecté RÉEL (x.com/ relevé le 2026-11-06) : <input id="landing-identifier" type="text"
                // autoComplete="username" … name="username"> dans <label aria-label="Email or username"> ; URL exacte
                landing: Object.assign({}, LANDING),
                landing_twitter: Object.assign({}, LANDING, { url: "https://twitter.com/" }),
                landing_host_case: Object.assign({}, LANDING, { url: "https://X.COM/" }),
                // routes de connexion hors « / » : INCHANGÉES (aucune signature d'accueil exigée)
                flow_no_sig: { id: "other", name: "text", formAction: null, formModes: [] },
                flow_signup_form: { formAction: "/i/flow/signup", formModes: [{ type: "hidden", valueAttr: "signup" }] } };
  for (const k of Object.keys(yes)) { expect("gate", "yes:" + k, v(yes[k]), true); }
  // accueil « / » : signature RÉELLE exigée (attributs STATIQUES seulement, jamais une valeur saisie) ; un champ
  // d'inscription rendu SUR PLACE à « / » n'est jamais éligible
  const noRoot = { root_no_sig: { url: "https://x.com/" }, id_other: { id: "signup-identifier" }, id_null: { id: null },
                   id_case: { id: "Landing-Identifier" }, name_email: { name: "email" }, name_null: { name: null },
                   no_form: { formAction: null, formModes: [] }, action_query: { formAction: ONB + "?mode=login" },
                   action_abs: { formAction: "https://x.com" + ONB }, action_slash: { formAction: ONB + "/" },
                   action_signup: { formAction: "/i/flow/signup" }, action_empty: { formAction: "" },
                   modes_none: { formModes: [] }, mode_signup: { formModes: [{ type: "hidden", valueAttr: "signup" }] },
                   mode_case: { formModes: [{ type: "hidden", valueAttr: "Login" }] },
                   mode_text: { formModes: [{ type: "text", valueAttr: "login" }] },
                   mode_null: { formModes: [{ type: "hidden", valueAttr: null }] },
                   modes_two: { formModes: [{ type: "hidden", valueAttr: "login" }, { type: "hidden", valueAttr: "signup" }] },
                   modes_dup: { formModes: [{ type: "hidden", valueAttr: "login" }, { type: "hidden", valueAttr: "login" }] },
                   modes_not_array: { formModes: "login" },
                   // forme canonique EXACTE : « ? » ou « # » nus (représentables dans href) refusés
                   bare_query: { url: "https://x.com/?" }, bare_hash: { url: "https://x.com/#" },
                   bare_both: { url: "https://x.com/?#" } };
  for (const k of Object.keys(noRoot)) {
    expect("gate", "no_root:" + k, k === "root_no_sig" ? v(noRoot[k]) : v(Object.assign({}, LANDING, noRoot[k])), false);
  }
  const no = { password: { type: "password" }, number: { type: "number" }, search: { type: "search" },
               cur_pw: { ac: "current-password" }, new_pw: { ac: "new-password" }, otp: { ac: "one-time-code" },
               ac_off: { ac: "off" }, ac_empty: { ac: "" }, ac_two: { ac: "username email" }, ac_cc: { ac: "cc-number" },
               ac_section: { ac: "section-a username" }, ac_tel_cc: { ac: "tel-country-code" },
               numeric: { inputmode: "numeric" }, decimal: { inputmode: " Decimal " }, maxlen6: { maxlength: 6 },
               maxlen8: { maxlength: 8 }, pattern: { pattern: "[0-9]*" }, danger: { danger: true },
               not_top: { top: false }, not_focused: { focused: false }, hidden: { visible: false },
               disabled: { disabled: true }, readonly: { readonly: true }, textarea: { tag: "TEXTAREA" },
               home: { url: "https://x.com/home" }, home_slash: { url: "https://x.com/home/" },
               // accueil « / » : seulement l'URL EXACTE (aucune requête, aucun fragment, https, hôte X exact)
               root_signup: { url: "https://x.com/?mode=signup" }, root_mode_login: { url: "https://x.com/?mode=login" },
               root_start: { url: "https://x.com/?start=phone" }, root_lang: { url: "https://x.com/?lang=en" },
               root_unexpected: { url: "https://x.com/?foo=1" }, root_empty_param: { url: "https://x.com/?mode" },
               root_hash: { url: "https://x.com/#signup" }, root_double_slash: { url: "https://x.com//" },
               root_http: { url: "http://x.com/" }, root_evil: { url: "https://x.com.evil.example/" },
               root_evil_prefix: { url: "https://evilx.com/" }, root_port: { url: "https://x.com:8443/" },
               root_creds: { url: "https://a:b@x.com/" }, root_flow_signup: { url: "https://x.com/i/flow/signup" },
               signup_mode: { url: "https://x.com/i/jf/onboarding/web?mode=signup" },
               signup_phone: { url: "https://x.com/i/jf/onboarding/web?mode=login&start=phone" },
               onb_two_modes: { url: "https://x.com/i/jf/onboarding/web?mode=login&mode=signup" },
               onb_no_mode: { url: "https://x.com/i/jf/onboarding/web" },
               flow_signup: { url: "https://x.com/i/flow/signup" }, login_signup: { url: "https://x.com/login?mode=signup" },
               http: { url: "http://x.com/i/flow/login" }, evil: { url: "https://x.com.evil.example/i/flow/login" },
               port: { url: "https://x.com:8443/i/flow/login" }, creds: { url: "https://a:b@x.com/i/flow/login" },
               garbage: { url: "::" } };
  const words = ["Password", "Enter your password", "Mot de passe", "Passcode", "Verification code",
                 "Enter your verification code", "Code de vérification", "Code de confirmation", "Confirmation code",
                 "OTP", "One-time code", "Code à usage unique", "PIN", "Enter PIN", "NIP", "2FA",
                 "Two-factor authentication", "Authentification à deux facteurs", "2-step verification",
                 "Passkey", "Use a passkey", "Clé d'accès", "Clés d’accès", "Backup code", "Code de secours",
                 "Recovery code", "Code de récupération", "Security key", "Clé de sécurité", "Authenticator app",
                 "Card number", "Numéro de carte", "CVV", "Paiement", "Payment", "Expiration", "Token", "Jeton",
                 "Confirm your identity", "Vérifie ton identité", "verification", "authentication"];
  for (const k of Object.keys(no)) { expect("gate", "no:" + k, v(no[k]), false); }
  for (const w of words) {
    expect("gate", "word:" + w, v({ texts: base.texts.concat([w]) }), false);
    expect("gate", "word_upper:" + w, v({ texts: [w.toUpperCase()] }), false);
  }
  // document déchiffré
  const sid = "S".repeat(22);
  const sec = "F".repeat(22);
  const id = (doc) => G.identOf(doc, sid, sec);
  expect("ident", "good", id({ v: 1, s: sid, f: sec, t: " alex@example.com " }), "alex@example.com");
  expect("ident", "strip", id({ v: 1, s: sid, f: sec, t: "al\u202eex\n\u200b\u2028\u0000" }), "alex");
  expect("ident", "max", id({ v: 1, s: sid, f: sec, t: "a".repeat(G.ID_MAX) }), "a".repeat(100));
  expect("ident", "astral_counted_once", id({ v: 1, s: sid, f: sec, t: "😀".repeat(100) }), "😀".repeat(100));
  const bad = { wrong_sid: { v: 1, s: "T".repeat(22), f: sec, t: "a" }, wrong_secret: { v: 1, s: sid, f: "G".repeat(22), t: "a" },
                short_secret: { v: 1, s: sid, f: "F", t: "a" }, extra: { v: 1, s: sid, f: sec, t: "a", x: 1 },
                missing: { v: 1, s: sid, f: sec }, v2: { v: 2, s: sid, f: sec, t: "a" }, vstr: { v: "1", s: sid, f: sec, t: "a" },
                t_num: { v: 1, s: sid, f: sec, t: 5 }, blank: { v: 1, s: sid, f: sec, t: " \u200b\n" },
                long: { v: 1, s: sid, f: sec, t: "a".repeat(101) }, arr: [1], nul: null };
  for (const k of Object.keys(bad)) { expect("ident", "bad:" + k, id(bad[k]), null); }
  // défense en profondeur : la page du téléphone (HTTP LAN) peut être altérée ; l'extension refuse ELLE-MÊME tout
  // identifiant qui ressemble à un code (4 à 9 chiffres, espaces/points/tirets seulement, chiffres Unicode compris)
  const t = (s) => id({ v: 1, s: sid, f: sec, t: s });
  for (const code of ["1234", "123456", " 123 456 ", "12.34.56", "1-2-3-4-5-6", "123456789", "１２３４５６", "٤٥٦٧٨٩",
                      "123\u200b456", "12 34\n56", "0000"]) {
    expect("ident", "code:" + JSON.stringify(code), t(code), null);
  }
  for (const ok of ["+1 514 555 1234", "5145551234", "+33612345678", "+123456", "514-555-1234", "alex123456", "123",
                    "@a1234", "a1234"]) {
    expect("ident", "not_code:" + ok, t(ok), ok);
  }
  // décision FINALE de remplissage (même champ au focus, document au focus, MÊME URL exacte, champ encore éligible)
  const href = "https://x.com/i/flow/login";
  const fb = { ident: "alex", same: true, docFocus: true, href: href, startHref: href, eligible: true };
  const fo = (o) => G.fillOk(Object.assign({}, fb, o));
  expect("fill", "base", fo({}), true);
  const nofill = { ident_null: { ident: null }, ident_empty: { ident: "" }, other_element: { same: false },
                   same_truthy: { same: 1 }, doc_unfocused: { docFocus: false }, doc_truthy: { docFocus: "yes" },
                   url_query: { href: href + "?step=2" }, url_hash: { href: href + "#x" },
                   url_other: { href: "https://x.com/home" }, url_missing: { href: undefined, startHref: undefined },
                   ineligible: { eligible: false }, eligible_truthy: { eligible: 1 } };
  for (const k of Object.keys(nofill)) { expect("fill", "no:" + k, fo(nofill[k]), false); }
  expect("fill", "null", G.fillOk(null), false);
}

// ------------------------------------------------------------------ placement (bloc <place> de xentry.js, TEL QUEL)
// Rectangles RÉELS de https://x.com/ déconnecté à 1920×1080 (relevés le 2026-11-07, Chromium) : champ
// landing-identifier [366,659,730,679], formulaire [356,627,740,757], Continuer [356,709,740,757], fournisseurs, CGU,
// pied de page ; QR natif « Scan to get the app » en bas à droite. Pastille et panneau : ENTIÈREMENT à l'écran (≥ 16 px
// de chaque bord), à ≥ 16 px de chaque obstacle, à DROITE du formulaire si possible ; null si aucune place.
function place() {
  const src = fs.readFileSync(XENTRY, "utf8");
  const a = src.indexOf("// <place>");
  const b = src.indexOf("// </place>");
  if (a < 0 || b < a) { failures.push({ group: "place", name: "markers" }); return; }
  const P = new Function(src.slice(a, b) + "\nreturn { placeBox, hits, EDGE, GAP };")();
  expect("place", "edge_gap", [P.EDGE, P.GAP], [16, 16]);
  const R = (l, t, r, b2) => ({ left: l, top: t, right: r, bottom: b2 });
  const W = 1920;
  const H = 1080;
  const box = (p, w, h) => R(p.left, p.top, p.left + w, p.top + h);
  const inside = (r, vw, vh) => r.left >= 16 && r.top >= 16 && r.right <= vw - 16 && r.bottom <= vh - 16;
  const clear = (r, obs) => obs.every((o) => !P.hits(r, o, 16));
  // hits : écart EXACT de 16 px = libre ; 15 px = touché ; chevauchement = touché
  expect("place", "hits_gap16", P.hits(R(0, 0, 10, 10), R(26, 0, 40, 10), 16), false);
  expect("place", "hits_gap15", P.hits(R(0, 0, 10, 10), R(25, 0, 40, 10), 16), true);
  expect("place", "hits_vertical15", P.hits(R(0, 0, 10, 10), R(0, 25, 10, 40), 16), true);
  expect("place", "hits_right16", P.hits(R(26, 0, 40, 10), R(0, 0, 10, 10), 16), false);
  expect("place", "hits_vertical16", P.hits(R(0, 0, 10, 10), R(0, 26, 10, 40), 16), false);
  expect("place", "hits_below16", P.hits(R(0, 26, 10, 40), R(0, 0, 10, 10), 16), false);
  expect("place", "hits_overlap", P.hits(R(0, 0, 10, 10), R(5, 5, 20, 20), 0), true);
  expect("place", "hits_touch0", P.hits(R(0, 0, 10, 10), R(10, 0, 20, 10), 0), false);
  const field = R(366, 659, 730, 679);
  const form = R(356, 627, 740, 757);
  const cont = R(356, 709, 740, 757);
  const getapp = R(1616, 750, 1880, 1040);
  const real = [field, form, R(356, 627, 740, 689), cont, R(356, 386, 740, 432), R(356, 452, 740, 498),
                R(356, 518, 740, 566), R(550, 779, 678, 793), R(428, 779, 733, 811), R(540, 797, 668, 811),
                R(491, 1044, 1340, 1068), getapp, R(1632, 790, 1864, 1022)];
  // pastille (380×61) : juste à droite du formulaire, centrée sur le champ
  const pill = P.placeBox(W, H, 380, 61, form, { left: form.right + 32, top: (field.top + field.bottom - 61) / 2 }, real);
  expect("place", "real_pill_exact", pill, { left: 772, top: 639 });
  const pr = pill && box(pill, 380, 61);
  expect("place", "real_pill_ok", !!pr && inside(pr, W, H) && clear(pr, real), true);
  // panneau QR vertical (421×717) : en haut, à droite du formulaire, jamais sur la pastille ni sur le QR natif
  const pan = P.placeBox(W, H, 421, 717, form, { left: form.right + 32, top: 16 }, real.concat(pr ? [pr] : []));
  expect("place", "real_panel_exact", pan, { left: 1168, top: 16 });
  const qr = pan && box(pan, 421, 717);
  expect("place", "real_panel_ok", !!qr && inside(qr, W, H) && clear(qr, real.concat([pr])), true);
  // bords : formulaire collé à gauche, à droite, en haut, en bas ; QR natif toujours en bas à droite
  const edges = { left: R(16, 500, 400, 630), right: R(1504, 300, 1904, 430), top: R(700, 16, 1084, 146),
                  bottom: R(700, 934, 1084, 1064), top_left: R(16, 16, 400, 146), bottom_right: R(1504, 600, 1904, 730) };
  for (const k of Object.keys(edges)) {
    const f = edges[k];
    const obs = [f, getapp];
    for (const [kind, w, h, top] of [["pill", 380, 61, (f.top + f.bottom - 61) / 2], ["panel", 421, 717, 16],
                                     ["panel_small", 320, 560, 16]]) {
      const p = P.placeBox(W, H, w, h, f, { left: f.right + 32, top: top }, obs);
      const r = p && box(p, w, h);
      expect("place", "edge:" + k + ":" + kind, !!r && inside(r, W, H) && clear(r, obs), true);
      // panneau (trop haut pour tenir au-dessus/au-dessous) : forcément À GAUCHE du formulaire collé à droite
      if (r && k === "right" && kind !== "pill") { expect("place", "edge_right_goes_left:" + kind, r.right <= f.left - 16, true); }
      if (r && (k === "left" || k === "top_left")) { expect("place", "edge_prefers_right:" + k + kind, r.left >= f.right + 16, true); }
    }
  }
  // préférence absurde (hors écran) : bornée, toujours entièrement à l'écran
  for (const [k, pref] of [["far_right", { left: 5000, top: 500 }], ["far_left", { left: -900, top: -300 }],
                           ["far_bottom", { left: 772, top: 9000 }], ["nan", { left: NaN, top: NaN }]]) {
    const p = P.placeBox(W, H, 380, 61, form, pref, real);
    const r = p && box(p, 380, 61);
    expect("place", "pref:" + k, !!r && inside(r, W, H) && clear(r, real), true);
  }
  // préférence : à DROITE du formulaire même si le point préféré est à gauche
  const mid = R(500, 400, 880, 600);
  const pl = P.placeBox(W, H, 380, 61, mid, { left: 100, top: 470 }, [mid]);
  expect("place", "prefers_right_of_scope", !!pl && pl.left >= mid.right + 16, true);
  // aucune place : null (jamais par-dessus) ; boîte plus grande que l'écran ; dimensions invalides
  expect("place", "no_room", P.placeBox(W, H, 380, 61, form, { left: 0, top: 0 }, [R(0, 0, W, H)]), null);
  expect("place", "no_room_narrow", P.placeBox(800, 600, 421, 717, form, { left: 0, top: 0 }, []), null);
  expect("place", "too_wide", P.placeBox(400, 600, 380, 61, form, { left: 0, top: 0 }, []), null);
  expect("place", "exact_fit", P.placeBox(412, 93, 380, 61, R(0, 0, 0, 0), { left: 0, top: 0 }, []), { left: 16, top: 16 });
  expect("place", "zero", P.placeBox(W, H, 0, 61, form, { left: 0, top: 0 }, []), null);
  expect("place", "nan_size", P.placeBox(W, H, NaN, 61, form, { left: 0, top: 0 }, []), null);
  // seule place libre : le coin bas droit (tout le reste couvert) -> trouvée EXACTEMENT, bornée à 16 px des bords
  const only = P.placeBox(W, H, 380, 61, form, { left: 772, top: 639 }, [R(0, 0, W, 987), R(0, 987, 1508, H)]);
  expect("place", "only_corner", only, { left: W - 16 - 380, top: H - 16 - 61 });
}

// ------------------------------------------------------------------ page du téléphone : VRAI xphone.js, DOM factice
// Courses Envoyer / Annuler : réponses du service rendues dans l'ordre choisi par le test (fetch et minuteries manuels).
function phonePage(jwk, fp, sid, f) {
  const els = {};
  const mk = (id, extra) => {
    els[id] = Object.assign({ id: id, hidden: false, disabled: false, textContent: "", value: "", attrs: {}, on: {},
      addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); },
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
      focus() {}, fire(type) { (this.on[type] || []).forEach((fn) => fn({ preventDefault() {} })); },
      click() { if (!this.disabled && !this.hidden) { this.fire("click"); } } }, extra || {});
  };
  mk("xentry", { attrs: { "data-s": sid, "data-c": "C".repeat(43) } });
  mk("xform", { hidden: true });
  mk("xid");
  mk("xsend");
  mk("xcancel", { hidden: true });
  mk("xerr", { hidden: true });
  mk("xstatus");
  const reqs = [];
  const timers = [];
  const sb = { btoa: (s) => Buffer.from(s, "latin1").toString("base64"),
               atob: (s) => Buffer.from(s, "base64").toString("latin1"), crypto: nodeCrypto.webcrypto,
               setTimeout: (fn) => { timers.push(fn); return timers.length; },
               document: { getElementById: (id) => els[id] || null },
               location: { hash: "#" + f + "." + fp, pathname: "/r/x", search: "?s=" + sid },
               history: { replaceState() { sb.location.hash = ""; } } };
  sb.window = sb;
  sb.fetch = (p, opts) => new Promise((resolve, reject) => {
    reqs.push({ path: String(p).split("?")[0], body: opts && opts.body ? JSON.parse(opts.body) : null,
                reply: (status, j) => resolve({ status: status, json: () => Promise.resolve(j) }),
                fail: () => reject(new Error("net")) });
  });
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(PI4, "ui", "muse.js"), "utf8"), sb, { filename: "muse.js" });
  vm.runInContext(fs.readFileSync(XPHONE, "utf8"), sb, { filename: "xphone.js" });
  const flush = async () => { for (let i = 0; i < 8; i++) { await new Promise((r) => setImmediate(r)); } };
  const last = (p) => reqs.filter((r) => r.path === p).slice(-1)[0];
  const tick = async () => { const fns = timers.splice(0); fns.forEach((fn) => fn()); await flush(); };
  const ui = () => ({ status: els.xstatus.textContent, err: els.xerr.textContent, form: !els.xform.hidden,
                      cancel: !els.xcancel.hidden, cancelDisabled: els.xcancel.disabled });
  return { els, reqs, flush, last, tick, ui, n: (p) => reqs.filter((r) => r.path === p).length };
}

async function phoneUi() {
  const subtle = nodeCrypto.webcrypto.subtle;
  const pair = await subtle.generateKey({ name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]),
                                          hash: "SHA-256" }, false, ["encrypt", "decrypt"]);
  const jwk = await subtle.exportKey("jwk", pair.publicKey);
  const fp = b64u(nodeCrypto.createHash("sha256").update(Buffer.from(jwk.n, "base64url")).digest());
  const sid = "S".repeat(22);
  const f = "F".repeat(22);
  const NOTHING = /Rien n'a été saisi/;
  const claimed = async () => {
    const P = phonePage(jwk, fp, sid, f);
    await P.flush();
    P.last("/r/x/claim").reply(200, { ok: true, n: jwk.n, e: "AQAB" });
    await P.flush();
    return P;
  };
  const submit = async (P) => {
    P.els.xid.value = "alex@example.com";
    P.els.xform.fire("submit");
    await P.flush();
  };
  // 1) Envoyer accepté (200) : Annuler caché ET désactivé AUSSITÔT ; plus aucun /r/x/cancel ; issue lue au service
  let P = await claimed();
  expect("ui", "claimed:cancel_offered", [P.ui().form, P.ui().cancel], [true, true]);
  await submit(P);
  expect("ui", "in_flight:cancel_still_offered", P.ui().cancel, true);
  P.last("/r/x/submit").reply(200, { ok: true });
  await P.flush();
  expect("ui", "sent:cancel_hidden", P.ui().cancel, false);
  expect("ui", "sent:cancel_disabled", P.ui().cancelDisabled, true);
  expect("ui", "sent:form_hidden", P.ui().form, false);
  P.els.xcancel.fire("click");   // tap déjà en file : le gestionnaire lui-même refuse
  await P.flush();
  expect("ui", "sent:no_cancel_request", P.n("/r/x/cancel"), 0);
  expect("ui", "sent:not_nothing", NOTHING.test(P.ui().status), false);
  await P.tick();
  P.last("/r/x/status").reply(200, { ok: true, state: "delivered" });
  await P.flush();
  expect("ui", "sent:delivered", /C'est fait/.test(P.ui().status), true);
  // 2) course : Annuler pendant l'envoi ; 200 de l'envoi PUIS 409 too_late -> « trop tard », puis issue réelle
  P = await claimed();
  await submit(P);
  P.els.xcancel.click();
  await P.flush();
  expect("ui", "race_late:cancel_sent", P.n("/r/x/cancel"), 1);
  P.last("/r/x/submit").reply(200, { ok: true });
  await P.flush();
  expect("ui", "race_late:cancel_hidden", [P.ui().cancel, P.ui().cancelDisabled], [false, true]);
  P.last("/r/x/cancel").reply(409, { ok: false, error: "too_late", state: "sent" });
  await P.flush();
  expect("ui", "race_late:too_late_text", /Trop tard/.test(P.ui().status), true);
  expect("ui", "race_late:not_nothing", NOTHING.test(P.ui().status), false);
  await P.tick();
  P.last("/r/x/status").reply(200, { ok: true, state: "delivered" });
  await P.flush();
  expect("ui", "race_late:delivered", /C'est fait/.test(P.ui().status), true);
  // 3) course : la réponse d'Annuler arrive AVANT celle de l'envoi : issue « delivered » gardée, jamais écrasée
  P = await claimed();
  await submit(P);
  P.els.xcancel.click();
  await P.flush();
  P.last("/r/x/cancel").reply(409, { ok: false, error: "over", state: "delivered" });
  await P.flush();
  P.last("/r/x/submit").reply(200, { ok: true });
  await P.flush();
  expect("ui", "race_over:delivered_kept", /C'est fait/.test(P.ui().status), true);
  expect("ui", "race_over:not_nothing", NOTHING.test(P.ui().status), false);
  expect("ui", "race_over:no_form", [P.ui().form, P.ui().cancel], [false, false]);
  // 4) Annuler avant tout envoi : le service CONFIRME l'annulation -> « rien n'a été saisi » est alors vrai
  P = await claimed();
  P.els.xcancel.click();
  await P.flush();
  expect("ui", "cancel:pending_disabled", P.ui().cancelDisabled, true);
  expect("ui", "cancel:not_yet_final", NOTHING.test(P.ui().status), false);
  P.last("/r/x/cancel").reply(200, { ok: true, state: "cancelled" });
  await P.flush();
  expect("ui", "cancel:confirmed", NOTHING.test(P.ui().status), true);
  expect("ui", "cancel:closed", [P.ui().form, P.ui().cancel], [false, false]);
  // 5) Annuler sans réponse (réseau) : jamais « rien n'a été saisi » ; Annuler de nouveau possible ; suivi continu
  P = await claimed();
  P.els.xcancel.click();
  await P.flush();
  P.last("/r/x/cancel").fail();
  await P.flush();
  expect("ui", "cancel_net:not_nothing", NOTHING.test(P.ui().status), false);
  expect("ui", "cancel_net:retry", [P.ui().cancel, P.ui().cancelDisabled], [true, false]);
  // 5 bis) réponse d'envoi perdue puis 409 already_submitted : l'envoi a abouti -> Annuler retiré, issue suivie
  P = await claimed();
  await submit(P);
  P.last("/r/x/submit").reply(409, { ok: false, error: "already_submitted" });
  await P.flush();
  expect("ui", "already:cancel_hidden", [P.ui().cancel, P.ui().cancelDisabled], [false, true]);
  expect("ui", "already:not_nothing", NOTHING.test(P.ui().status), false);
  await P.tick();
  P.last("/r/x/status").reply(200, { ok: true, state: "delivered" });
  await P.flush();
  expect("ui", "already:delivered", /C'est fait/.test(P.ui().status), true);
  // 6) issues finales distinctes (contenu rejeté / champ refusé) : jamais le même texte
  for (const [state, re] of [["rejected", /rejet/i], ["refused", /champ/]]) {
    P = await claimed();
    await submit(P);
    P.last("/r/x/submit").reply(200, { ok: true });
    await P.flush();
    await P.tick();
    P.last("/r/x/status").reply(200, { ok: true, state: state });
    await P.flush();
    expect("ui", "final:" + state, re.test(P.ui().status), true);
    expect("ui", "final_not_nothing:" + state, NOTHING.test(P.ui().status), false);   // rapporté par la TV, après remise
  }
  const UNSURE = /incertain/i;
  // 7) course réelle : envoi ACCEPTÉ, réponse perdue (réseau) ; la TV livre ; le renvoi reçoit 410 over + issue réelle
  P = await claimed();
  await submit(P);
  P.last("/r/x/submit").fail();
  await P.flush();
  expect("ui", "lost:uncertain", [UNSURE.test(P.ui().status), NOTHING.test(P.ui().status)], [true, false]);
  expect("ui", "lost:retry_possible", [P.ui().form, P.els.xsend.disabled], [true, false]);
  await submit(P);
  expect("ui", "lost:retry_sent", P.n("/r/x/submit"), 2);
  P.last("/r/x/submit").reply(410, { ok: false, error: "over", state: "delivered" });
  await P.flush();
  expect("ui", "lost:delivered", /C'est fait/.test(P.ui().status), true);
  expect("ui", "lost:not_nothing", NOTHING.test(P.ui().status), false);
  expect("ui", "lost:cancel_hidden_no_form", [P.ui().cancel, P.ui().cancelDisabled, P.ui().form], [false, true, false]);
  // 7 bis) même course, renvoi pendant la livraison : 409 already_submitted -> suivi, puis issue lue au service
  P = await claimed();
  await submit(P);
  P.last("/r/x/submit").fail();
  await P.flush();
  await submit(P);
  P.last("/r/x/submit").reply(409, { ok: false, error: "already_submitted" });
  await P.flush();
  expect("ui", "lost_already:cancel_hidden", [P.ui().cancel, P.ui().cancelDisabled], [false, true]);
  // 8) issue impossible à connaître : jamais « rien n'a été saisi » (410 générique, 5xx, illisible, renvoi refusé
  //    après un envoi incertain, issue « unknown » / « gone » du service)
  for (const [name, status, j] of [["410_generic", 410, { ok: false, error: "expired" }], ["500", 500, {}],
                                   ["garbage", 409, { ok: false, error: "over", state: "constructor" }]]) {
    P = await claimed();
    await submit(P);
    P.last("/r/x/submit").reply(status, j);
    await P.flush();
    expect("ui", "unknowable:" + name, [UNSURE.test(P.ui().status), NOTHING.test(P.ui().status)], [true, false]);
  }
  P = await claimed();
  await submit(P);
  P.last("/r/x/submit").fail();
  await P.flush();
  await submit(P);
  P.last("/r/x/submit").reply(400, { ok: false, error: "bad_ct" });
  await P.flush();
  expect("ui", "retry_refused_after_unsure", [UNSURE.test(P.ui().status), NOTHING.test(P.ui().status)], [true, false]);
  for (const state of ["unknown", "gone"]) {
    P = await claimed();
    await submit(P);
    P.last("/r/x/submit").fail();
    await P.flush();
    await P.tick();
    P.last("/r/x/status").reply(200, { ok: true, state: state });
    await P.flush();
    expect("ui", "status_" + state, [UNSURE.test(P.ui().status), NOTHING.test(P.ui().status), P.ui().form],
           [true, false, false]);
  }
  // 9) refus CONFIRMÉ par le service avant toute remise, sans envoi incertain : « rien n'a été saisi » est vrai
  P = await claimed();
  await submit(P);
  P.last("/r/x/submit").reply(400, { ok: false, error: "bad_ct" });
  await P.flush();
  expect("ui", "pre_handoff_refused", [NOTHING.test(P.ui().status), P.ui().form], [true, false]);
}

(async () => {
  try {
    await phone();
    gate();
    place();
    await phoneUi();
  } catch (err) {
    failures.push({ group: "crash", name: String(err && err.stack || err).slice(0, 400) });
  }
  process.stdout.write(JSON.stringify({ ok: failures.length === 0, failures: failures.slice(0, 20), counts }) + "\n");
  process.exit(failures.length === 0 ? 0 : 1);
})();
