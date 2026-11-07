/* Harnais déterministe de la saisie de l'identifiant X par téléphone (tests seulement, jamais installé).
   argv[2] = dossier scripts/pi4 ; argv[3] = xphone.js à tester (défaut ui/xphone.js) ; argv[4] = xentry.js à tester
   (défaut x-tv-extension/xentry.js) — les tests de mutation passent des copies altérées.
   A. Téléphone : VRAIS ui/muse.js + ui/xphone.js dans un contexte vm (window, sans document) : fragment, vérification de
      la clé (empreinte du QR, e = 65537, module de 2048 bits), refus (vide, code, trop long), nettoyage, chiffré de
      taille FIXE, aléatoire, déchiffré par WebCrypto (clé NON exportable, comme l'extension) et par OpenSSL.
   B. Extension : bloc <gate> … </gate> de x-tv-extension/xentry.js exécuté TEL QUEL : URL de connexion, faits du champ
      (mot de passe, OTP, code, NIP, 2FA, passkey, secours, paiement EN/FR jamais éligibles), document déchiffré.
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
const counts = { phone: 0, gate: 0, ident: 0 };
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
  const G = new Function(src.slice(a, b) + "\nreturn { loginUrl, factsOk, identOf, ID_MAX };")();
  const base = { top: true, focused: true, visible: true, tag: "INPUT", disabled: false, readonly: false,
                 url: "https://x.com/i/flow/login", type: "text", ac: "username", inputmode: "", maxlength: -1,
                 pattern: "", danger: false, texts: ["text", "", "", "", "", "", "Phone, email, or username",
                                                      "Sign in to X"] };
  const v = (o) => G.factsOk(Object.assign({}, base, o));
  expect("gate", "base", v({}), true);
  const yes = { webauthn: { ac: "username webauthn" }, email: { ac: "email", type: "email" }, tel: { ac: "tel", type: "tel" },
                tel_national: { ac: "tel-national", type: "tel" }, tel_local: { ac: "TEL-LOCAL", type: "tel" },
                onboarding: { url: "https://x.com/i/jf/onboarding/web?mode=login&redirect_after_login=%2F" },
                twitter: { url: "https://twitter.com/login" }, mobile: { url: "https://mobile.x.com/i/flow/login" },
                www: { url: "https://www.x.com/i/flow/login/" }, maxlen_long: { maxlength: 512 },
                fr_label: { texts: ["Téléphone, adresse e-mail ou nom d'utilisateur", "Connexion à X"] } };
  for (const k of Object.keys(yes)) { expect("gate", "yes:" + k, v(yes[k]), true); }
  const no = { password: { type: "password" }, number: { type: "number" }, search: { type: "search" },
               cur_pw: { ac: "current-password" }, new_pw: { ac: "new-password" }, otp: { ac: "one-time-code" },
               ac_off: { ac: "off" }, ac_empty: { ac: "" }, ac_two: { ac: "username email" }, ac_cc: { ac: "cc-number" },
               ac_section: { ac: "section-a username" }, ac_tel_cc: { ac: "tel-country-code" },
               numeric: { inputmode: "numeric" }, decimal: { inputmode: " Decimal " }, maxlen6: { maxlength: 6 },
               maxlen8: { maxlength: 8 }, pattern: { pattern: "[0-9]*" }, danger: { danger: true },
               not_top: { top: false }, not_focused: { focused: false }, hidden: { visible: false },
               disabled: { disabled: true }, readonly: { readonly: true }, textarea: { tag: "TEXTAREA" },
               home: { url: "https://x.com/home" }, landing: { url: "https://x.com/" },
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
}

(async () => {
  try {
    await phone();
    gate();
  } catch (err) {
    failures.push({ group: "crash", name: String(err && err.stack || err).slice(0, 400) });
  }
  process.stdout.write(JSON.stringify({ ok: failures.length === 0, failures: failures.slice(0, 20), counts }) + "\n");
  process.exit(failures.length === 0 ? 0 : 1);
})();
