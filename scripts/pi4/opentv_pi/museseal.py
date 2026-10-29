"""Chiffrement applicatif du jeton SDK Muse entre le téléphone et la boîte (stdlib seulement).

Pourquoi : la page du téléphone est servie en HTTP sur le réseau local (aucun certificat propre à l'appareil). Un jeton
envoyé en clair serait lisible par tout appareil qui écoute le Wi-Fi. Chaque séance de connexion Muse tire donc une
paire RSA-2048 NEUVE, gardée en mémoire seulement le temps de la séance ; le téléphone chiffre (RSA-OAEP, SHA-256,
MGF1-SHA-256, étiquette vide) un court document {v, s, f, t} avec la clé publique, et seule la boîte peut le lire.

Limites (dites telles quelles, voir README) : cela protège contre l'écoute passive du réseau local. Un attaquant ACTIF
sur le LAN (usurpation ARP) pourrait remplacer la page et son script, servis en HTTP : aucune cryptographie livrée par
un canal non authentifié ne l'empêche. Le secret « f » du QR (fragment d'URL, jamais envoyé en clair) lie l'envoi au
téléphone qui a scanné l'écran de la TV : un écouteur passif qui verrait l'adresse ne peut pas forger d'envoi valide.

WebCrypto (crypto.subtle) n'existe que dans un « contexte sécurisé » (HTTPS ou localhost) : jamais sur
http://<adresse LAN>. Le téléphone utilise donc ui/muse.js (RSA-OAEP en BigInt, SHA-256 en JavaScript, aléa de
crypto.getRandomValues, disponible hors contexte sécurisé) ; la boîte vérifie ici, en temps borné, avec pow() natif.
Interopérabilité testée contre la bibliothèque `cryptography` et contre muse.js exécuté par Node.
"""
import base64
import hashlib
import hmac
import secrets

BITS = 2048
E = 65537
HLEN = 32
# petits premiers pour le crible avant Miller-Rabin (génération de clé rapide sur une Pi 4)
_SMALL = []
for _n in range(3, 2000, 2):
    if all(_n % _p for _p in _SMALL if _p * _p <= _n):
        _SMALL.append(_n)
_MR_ROUNDS = 40   # probabilité d'erreur < 4^-40 par premier (bien au-delà des recommandations FIPS 186-5)


class SealError(ValueError):
    """Déchiffrement refusé : un seul message, sans détail (aucun oracle de remplissage)."""

    def __init__(self):
        super().__init__("seal_rejected")


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_dec(text, cap):
    if not isinstance(text, str) or not 0 < len(text) <= cap:
        raise SealError()
    if text.strip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"):
        raise SealError()
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise SealError()


def _i2b(x, n):
    return x.to_bytes(n, "big")


def _b2i(b):
    return int.from_bytes(b, "big")


def _probable_prime(n, rand):
    if n < 2:
        return False
    for p in _SMALL:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(_MR_ROUNDS):
        a = rand.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits, rand):
    """Premier aléatoire de `bits` bits, deux bits forts à 1 (n = p·q fait exactement 2·bits bits), p ≢ 1 (mod e)."""
    while True:
        c = rand.getrandbits(bits) | (3 << (bits - 2)) | 1
        if c % E == 1:
            continue
        # crible incrémental : saute les multiples de petits premiers sans Miller-Rabin
        for step in range(0, 4096, 2):
            x = c + step
            if x.bit_length() != bits:
                break
            if any(x % p == 0 for p in _SMALL):
                continue
            if x % E != 1 and _probable_prime(x, rand):
                return x


def _mgf1(seed, length):
    out, i = b"", 0
    while len(out) < length:
        out += hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:length]


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


class PrivateKey:
    """Clé privée d'UNE séance (mémoire seulement). repr/str ne révèlent rien ; wipe() la rend inutilisable."""

    def __init__(self, bits=BITS, rand=None):
        rand = rand or secrets.SystemRandom()
        half = bits // 2
        while True:
            p, q = _prime(half, rand), _prime(half, rand)
            if p == q:
                continue
            n = p * q
            if n.bit_length() != bits:
                continue
            lam = (p - 1) * (q - 1) // _gcd(p - 1, q - 1)
            if _gcd(E, lam) != 1:
                continue
            d = pow(E, -1, lam)
            break
        if p < q:
            p, q = q, p
        self.n, self.e, self._d, self._p, self._q = n, E, d, p, q
        self._dp, self._dq, self._qinv = d % (p - 1), d % (q - 1), pow(q, -1, p)
        self.k = (n.bit_length() + 7) // 8

    def __repr__(self):
        return "<museseal.PrivateKey live=%s>" % (self._d is not None)

    __str__ = __repr__

    def public_jwk(self):
        """Clé publique (JWK RSA) pour la page du téléphone : n et e en base64url, sans aucun élément privé."""
        return {"kty": "RSA", "alg": "RSA-OAEP-256", "n": b64u(_i2b(self.n, self.k)), "e": b64u(_i2b(self.e, 3))}

    def fingerprint(self):
        return b64u(hashlib.sha256(_i2b(self.n, self.k)).digest())[:22]

    def wipe(self):
        self._d = self._p = self._q = self._dp = self._dq = self._qinv = None

    def decrypt(self, ct, label=b""):
        """RSA-OAEP (SHA-256/MGF1-SHA-256) -> octets clairs ; SealError (message unique) sinon."""
        if self._d is None or not isinstance(ct, (bytes, bytearray)) or len(ct) != self.k:
            raise SealError()
        c = _b2i(ct)
        if c >= self.n:
            raise SealError()
        m1 = pow(c, self._dp, self._p)
        m2 = pow(c, self._dq, self._q)
        h = (self._qinv * (m1 - m2)) % self._p
        m = m2 + h * self._q
        if pow(m, self.e, self.n) != c:   # contrôle de faute (CRT)
            raise SealError()
        em = _i2b(m, self.k)
        lhash = hashlib.sha256(label).digest()
        y, mseed, mdb = em[0], em[1:1 + HLEN], em[1 + HLEN:]
        seed = _xor(mseed, _mgf1(mdb, HLEN))
        db = _xor(mdb, _mgf1(seed, self.k - HLEN - 1))
        # vérifications sans branche précoce visible (un seul refus à la fin)
        good = hmac.compare_digest(db[:HLEN], lhash) & (y == 0)
        rest = db[HLEN:]
        idx, found = -1, False
        for i, b in enumerate(rest):
            if not found and b == 1:
                idx, found = i, True
            elif not found and b != 0:
                good = False
                found = True
        if not good or idx < 0:
            raise SealError()
        return bytes(rest[idx + 1:])


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def public_from_jwk(jwk):
    """-> (n, e) d'un JWK RSA public (tests / outils) ; ValueError sinon."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
        raise ValueError("jwk")
    return _b2i(b64u_dec(jwk.get("n"), 1024)), _b2i(b64u_dec(jwk.get("e"), 8))


def encrypt(n, e, msg, label=b"", rand=None):
    """RSA-OAEP (SHA-256) — même calcul que ui/muse.js ; sert aux tests et aux outils, jamais au chemin du service."""
    rand = rand or secrets.SystemRandom()
    k = (n.bit_length() + 7) // 8
    if len(msg) > k - 2 * HLEN - 2:
        raise ValueError("message_too_long")
    lhash = hashlib.sha256(label).digest()
    db = lhash + b"\x00" * (k - len(msg) - 2 * HLEN - 2) + b"\x01" + msg
    seed = bytes(rand.getrandbits(8) for _ in range(HLEN))
    mdb = _xor(db, _mgf1(seed, k - HLEN - 1))
    mseed = _xor(seed, _mgf1(mdb, HLEN))
    return _i2b(pow(_b2i(b"\x00" + mseed + mdb), e, n), k)
