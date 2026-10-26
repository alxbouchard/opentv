"""Réseaux Wi-Fi proches pour l'assistant de configuration (lecture seule, sans privilège).

Une seule commande, argv fixe, sans shell : `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list --rescan no` (lit la
liste que NetworkManager tient déjà ; aucun balayage forcé, aucune autorisation polkit). Sortie validée strictement :
SSID conforme à validate.ssid, signal entier 0..100, sécurité WPA personnelle seulement (le formulaire n'accepte
qu'un mot de passe WPA ; réseaux ouverts, WEP, entreprise 802.1X ou WPA3 seul exclus), point d'accès OpenTV exclu,
doublons fusionnés (meilleur signal), liste bornée. Aucun mot de passe ni BSSID n'est lu.

Coût borné quel que soit le nombre de requêtes : résultat (même vide ou en échec) mis en cache, au plus une
exécution par MIN_INTERVAL_S, une seule à la fois (les requêtes concurrentes reçoivent le cache, sans attendre).

Wi-Fi temporaire : quand wlan0 diffuse le point d'accès OpenTV, il ne voit plus les autres réseaux. La dernière liste
non vide (au plus STALE_MAX_S, relevée par App avant le démarrage du point d'accès) peut alors être proposée.
"""
import re
import threading
import time

from . import validate
from .netstate import _split_terse

NMCLI_WIFI_LIST = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no"]
TIMEOUT_S = 3
CACHE_TTL_S = 20.0
MIN_INTERVAL_S = 5.0
MAX_NETWORKS = 8
MAX_LINES = 256
STALE_MAX_S = 1800.0
OWN_AP_RE = re.compile(r"OpenTV-[A-Z0-9]{4}")
SECURITY_TOKENS = frozenset(("WPA1", "WPA2", "WPA3", "802.1X", "WEP", "OWE", "OWE-TM", "--"))


def _personal_wpa(sec):
    toks = sec.split()
    if not toks or not set(toks) <= SECURITY_TOKENS:
        return False   # valeur inconnue : exclue (fail-closed)
    return ("WPA1" in toks or "WPA2" in toks) and "802.1X" not in toks


def bars(signal):
    """Signal 0..100 -> 1..4 barres (affichage)."""
    return 4 if signal >= 75 else 3 if signal >= 50 else 2 if signal >= 25 else 1


def parse(text):
    """Sortie nmcli -t -> [{"ssid", "signal"}] validée, dédupliquée, triée (signal décroissant), bornée."""
    text = text if isinstance(text, str) else ""
    lines = text.split("\n")
    if lines and lines[-1] != "":
        lines = lines[:-1]   # dernière ligne non terminée : peut-être tronquée par le lanceur, ignorée
    best = {}
    for line in lines[:MAX_LINES]:
        f = _split_terse(line)
        if len(f) != 3:
            continue
        ssid, sig, sec = f
        if ssid in ("", "--") or OWN_AP_RE.fullmatch(ssid):
            continue
        try:
            validate.ssid(ssid)
        except ValueError:
            continue
        if not re.fullmatch(r"[0-9]{1,3}", sig) or int(sig) > 100:
            continue
        if not _personal_wpa(sec):
            continue
        if int(sig) > best.get(ssid, -1):
            best[ssid] = int(sig)
    out = sorted(best.items(), key=lambda kv: (-kv[1], kv[0].casefold(), kv[0]))
    return [{"ssid": s, "signal": v} for s, v in out[:MAX_NETWORKS]]


class Scanner:
    def __init__(self, runner, mono=time.monotonic):
        self.runner, self.mono = runner, mono
        self._lock = threading.Lock()   # une seule exécution à la fois
        self._data_lock = threading.Lock()
        self._cache, self._at, self._ran_at = [], -1e9, -1e9
        self._good, self._good_at = [], -1e9   # dernière liste non vide (repli pendant le Wi-Fi temporaire)

    def networks(self, allow_stale=False):
        """-> liste (copie) des réseaux ; exécute nmcli seulement si le cache a expiré et le débit le permet.
        allow_stale : liste vide => dernière liste non vide de moins de STALE_MAX_S."""
        nets = self._networks()
        if nets or not allow_stale:
            return nets
        with self._data_lock:
            if 0 <= self.mono() - self._good_at <= STALE_MAX_S:
                return [dict(n) for n in self._good]
        return []

    def _networks(self):
        now = self.mono()
        with self._data_lock:
            fresh = now - self._at < CACHE_TTL_S
            throttled = now - self._ran_at < MIN_INTERVAL_S
            cached = [dict(n) for n in self._cache]
        if fresh or throttled or not self._lock.acquire(blocking=False):
            return cached
        try:
            with self._data_lock:
                self._ran_at = self.mono()
            try:
                rc, out = self.runner(list(NMCLI_WIFI_LIST), TIMEOUT_S)
            except Exception:   # lanceur défaillant : liste vide, jamais d'erreur côté page
                rc, out = 1, ""
            nets = parse(out) if rc == 0 else []
            with self._data_lock:
                self._cache, self._at = nets, self.mono()
                if nets:
                    self._good, self._good_at = nets, self._at
            return [dict(n) for n in nets]
        finally:
            self._lock.release()
