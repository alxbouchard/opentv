"""Orchestrateur du service OpenTV Pi : mode configuration (QR + jeton), hotspot borné, messages d'agents, « Pour toi ».

Aucun privilège : les changements réseau passent par des jobs 0600 consommés par le helper root.
"""
import json
import os
import secrets
import string
import threading
import time

from . import VERSION, agents, bootid, cec, config, fsutil, netjobs, netstate, nethelper, peeruid, validate, wifiscan
from .foryou import ForYou, EngineError
from .setup_token import SetupToken

KNOWN_AGENTS = ("muse", "hermit", "hermes")
AGENT_LEVELS = {"none": [], "read": ["read"], "feedback": ["feedback", "read"],
                "full": ["display", "feedback", "read"]}
AP_PSK_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"
AP_RETRY_S = 120.0
FORYOU_TTL_S = 120.0
# Échéance d'un wifi-apply côté service : le helper refuse un job plus vieux que WIFI_APPLY_PICKUP_MAX_S et termine
# toute exécution en RUN_BUDGET_S ; au-delà, le job est abandonné (échec honnête, résultat tardif ignoré).
APPLY_DEADLINE_S = nethelper.WIFI_APPLY_PICKUP_MAX_S + nethelper.RUN_BUDGET_S + 30.0
STALE_JOBS_MAX = 32
CEC_REFRESH_S = 300.0
SCAN_PRIME_S = 15.0           # hors ligne, avant le Wi-Fi temporaire : relevé des réseaux proches (thread, borné)
# Soumissions avec un jeton faux (jamais émis) : sans effet sur le QR (256 bits, aucun essai à deviner), mais bornées
# par adresse IP pour ne pas laisser un appareil du LAN occuper le service ; table bornée.
BAD_TOKEN_PER_IP = 30
BAD_TOKEN_WINDOW_S = 60.0
BAD_TOKEN_TRACKED_IPS = 256
ERR_FR = {
    "wifi_connect_failed": "Le Wi-Fi a refusé la connexion : vérifie le nom du réseau et le mot de passe, "
                           "puis scanne le nouveau QR.",
    "ap_start_failed": "Impossible de créer le Wi-Fi temporaire OpenTV. Branche un câble Ethernet à la boîte.",
    "apply_timeout": "La connexion au Wi-Fi n'a pas répondu à temps. Vérifie le nom du réseau et le mot de passe, "
                     "puis scanne le nouveau QR.",
}
# Erreurs du formulaire QR (texte affiché tel quel ; web.py en déduit l'étape de l'assistant à réafficher)
ERR_FORM_NAME = "Nom de la TV invalide : lettres, chiffres, espaces, apostrophes ou tirets (32 au maximum)."
ERR_FORM_NET = "Choisis comment la TV se connecte."
ERR_FORM_OFFLINE = "La TV n'a pas encore Internet : choisis ton Wi-Fi."
ERR_FORM_SSID = "Nom du Wi-Fi invalide (1 à 32 caractères, sans « ; »)."
ERR_FORM_PSK = "Mot de passe Wi-Fi invalide : il faut entre 8 et 63 caractères."
ERR_FORM_AGENT = "Choix d'agent invalide."
MSG_NO_NETWORK = ("Branche un câble Ethernet à la boîte OpenTV (recommandé). Sans câble, un Wi-Fi temporaire "
                  "« OpenTV » apparaîtra dans une minute.")
MSG_NO_NETWORK_NO_AP = "Branche un câble Ethernet à la boîte OpenTV pour la configurer."
MSG_BOOT_UNKNOWN = ("Wi-Fi temporaire OpenTV désactivé par sécurité (démarrage de la boîte non identifiable). "
                    "Branche un câble Ethernet à la boîte OpenTV pour la configurer.")
MSG_HOTSPOT_EXPIRED = ("Le Wi-Fi temporaire OpenTV est fermé (durée maximale atteinte). Branche un câble Ethernet à la "
                       "boîte OpenTV, ou redémarre-la (débrancher puis rebrancher) pour réessayer.")


def _noop_log(event, **fields):
    pass


def _boot_id(proc_root):
    """Identifiant du démarrage courant ; "" = inconnu ⇒ fail-closed (voir bootid.py)."""
    return bootid.read(proc_root)


class App:
    def __init__(self, cfg, runner=nethelper.default_runner, mono=time.monotonic, log=_noop_log, cec_root="/",
                 proc_root="/proc"):
        self.cfg, self.runner, self.mono, self.log, self.cec_root = cfg, runner, mono, log, cec_root
        self.lock = threading.RLock()
        self.fy_lock = threading.Lock()   # « Pour toi » (SQLite, catalogue) : jamais sous self.lock, que tick() prend
        self.tv_key = secrets.token_urlsafe(32)
        # page TV et API réservées au compte du kiosque (UID du pair TCP) ; None = contrôle indisponible (hors Linux)
        self.tv_client_uids = peeruid.allowed_uids(cfg.tv_client_user, proc_root)
        self.peer_uid_fn = lambda ip, port, sport: peeruid.tcp_peer_uid(ip, port, sport, proc_root)
        if self.tv_client_uids is None:
            log("tv_peer_check_unavailable")
        self.token = SetupToken(clock=mono, ttl=cfg.token_ttl_s)
        self.wifi_scan = wifiscan.Scanner(runner, mono)   # réseaux proches (assistant QR) : borné, mis en cache
        self.registry = agents.Registry(cfg.registry_file)
        self.audit = agents.Audit(cfg.audit_file)
        try:
            self.foryou = ForYou(cfg.engine_dir, cfg.foryou_state, cfg.catalogue_file, cfg.source_id, log=log)
        except (EngineError, ValueError) as e:
            self.foryou = None
            log("foryou_unavailable", error=str(e)[:40])
        self.net = {"mode": "offline", "ip": None, "wifi_present": False, "nm_ok": False}
        self.offline_since = mono()
        self.ap, self.ap_retry_at = None, 0.0
        self.pending = {}
        self.stale_jobs = []          # jobs abandonnés (échéance) : leur résultat tardif est ignoré
        self.apply = None             # {"job", "deadline"} : wifi-apply en cours, échéance monotone persistée
        self.phase, self.phase_msg = "waiting", None
        self.message = None
        self.lan_port_actual = None
        self.bad_token_ips = {}       # ip -> [horodatages monotones] des POST à jeton faux (borné)
        self.fy_session, self.fy_at = None, -1e9
        self.cec, self.cec_at = {"adapters": [], "discovery_only": True}, -1e9
        self._cec_thread = None
        self._scan_thread, self.scan_prime_at = None, -1e9
        self.ap_stop_at = -1e9
        self.boot_id = _boot_id(proc_root)
        self.hs = self._hs_load()
        self.device = None
        self.setup_mode = True
        self.reload_device()
        self._apply_load()

    # -- état de l'appareil
    def reload_device(self):
        with self.lock:
            self.device = config.load_device(self.cfg)
            self.setup_mode = self.device is None or os.path.exists(self.cfg.setup_flag)
            if self.setup_mode and self.token.needs_rotation():
                self.token.issue()

    def close(self):
        if self.foryou is not None:
            self.foryou.close()

    def _touch_setup_flag(self):
        try:   # sans suivre de lien ni bloquer sur une FIFO ; fichier régulier exigé
            fsutil.touch_regular(self.cfg.setup_flag, 0o600)
        except OSError as e:
            self.log("setup_flag_error", error=getattr(e, "reason", type(e).__name__))

    def request_setup(self):
        self._touch_setup_flag()
        with self.lock:
            self._hs_rearm("tv")   # demande explicite depuis la TV : le Wi-Fi temporaire peut de nouveau servir
        self.log("setup_requested", origin="tv")

    def _finish_setup(self):
        self.setup_mode, self.phase, self.phase_msg = False, "done", None
        self.token.burn()
        try:
            os.remove(self.cfg.setup_flag)
        except FileNotFoundError:
            pass
        self.log("setup_done", network=self.net["mode"])
        # toute réussite ferme le Wi-Fi temporaire, même si Ethernet le masque et que la connexion est « gardée »
        if self.ap is not None or self.net.get("ap_active"):
            self._stop_ap("setup_done", force=True)
        self.ap = None

    # -- Wi-Fi temporaire : budget borné par démarrage de la boîte, persistant à travers les redémarrages du service
    def _hs_fresh(self):
        # démarrage non identifiable : budget considéré épuisé (aucun hotspot automatique), jamais réarmé
        return {"v": 1, "boot_id": self.boot_id, "first_start": None, "expired": not self.boot_id}

    def _hs_load(self):
        if not self.boot_id:
            self.log("hotspot_disabled", reason="boot_id_unknown")
            return self._hs_fresh()
        try:
            with open(self.cfg.hotspot_state, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("v") != 1 or d.get("boot_id") != self.boot_id:
                raise ValueError("autre démarrage")
            fs = d.get("first_start")
            if fs is not None and (not isinstance(fs, (int, float)) or isinstance(fs, bool)):
                raise ValueError("first_start")
            return {"v": 1, "boot_id": self.boot_id, "first_start": fs, "expired": d.get("expired") is True}
        except FileNotFoundError:
            return self._hs_fresh()
        except (OSError, ValueError):
            return self._hs_fresh()   # nouveau démarrage (boot_id différent) ou fichier illisible : budget neuf

    def _hs_save(self):
        try:
            agents.atomic_write_json(self.cfg.hotspot_state, self.hs)
        except OSError as e:   # l'état mémoire reste la référence : un échec d'écriture ne réarme jamais
            self.log("hotspot_state_error", error=type(e).__name__)

    def _hs_rearm(self, origin):
        if not self.boot_id:   # identité de démarrage inconnue : budget non persistable, le hotspot reste fermé
            self.log("hotspot_rearm_refused", origin=origin, reason="boot_id_unknown")
            return
        self.hs = self._hs_fresh()
        self.ap_retry_at = 0.0
        if self.ap is not None or self.net.get("ap_active"):
            # un AP diffuse encore : son budget neuf commence maintenant (la garde root le lit dans hotspot.json)
            self.hs["first_start"] = self.mono()
            self._hs_save()
        else:
            try:
                os.remove(self.cfg.hotspot_state)
            except FileNotFoundError:
                pass
        self.log("hotspot_rearmed", origin=origin)

    def _stop_ap(self, reason, force=False):
        if "ap-stop" in self.pending.values():
            return
        if not force and self.mono() - self.ap_stop_at < AP_RETRY_S:
            return
        self.ap_stop_at = self.mono()
        self._submit("ap-stop", {})
        self.log("hotspot_stop", reason=reason)

    def _hotspot_guard(self):
        """Fail-closed : expiration du budget, AP inconnu (orphelin) ou configuration terminée ⇒ ap-stop."""
        now = self.mono()
        if os.path.exists(self.cfg.hotspot_rearm):   # opentv-pi setup reset (root) : réarmement explicite
            try:
                os.remove(self.cfg.hotspot_rearm)
            except OSError:
                pass
            self._hs_rearm("cli")
        hs = self.hs
        # durée écoulée négative (horloge d'un autre démarrage) = budget invalide : fail-closed, comme l'échéance
        if hs["first_start"] is not None and not hs["expired"] and \
                not 0 <= now - hs["first_start"] < self.cfg.hotspot_max_s:
            hs["expired"] = True
            self._hs_save()
            self.log("hotspot_expired", seconds=int(self.cfg.hotspot_max_s))
            if self.ap is not None or self.net.get("ap_active") or "ap-start" in self.pending.values():
                self._stop_ap("expired", force=True)
            self.ap = None
        if not self.net.get("ap_active"):
            return
        if not self.setup_mode:
            self._stop_ap("configured")
        elif hs["expired"]:
            self._stop_ap("expired")
        elif self.ap is None:
            self._stop_ap("orphan")   # point d'accès d'une exécution précédente : identifiants inconnus, jamais affiché

    # -- wifi-apply : échéance bornée et persistée (même démarrage de la boîte), jamais « applying » indéfiniment
    def _apply_save(self):
        try:
            if self.apply is None:
                os.remove(self.cfg.apply_state)
            else:
                agents.atomic_write_json(self.cfg.apply_state, {"v": 1, "boot_id": self.boot_id,
                                                                "job": self.apply["job"],
                                                                "deadline": self.apply["deadline"]})
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log("apply_state_error", error=type(e).__name__)

    def _apply_clear(self):
        if self.apply is not None:
            self.apply = None
            self._apply_save()

    def _apply_load(self):
        """Redémarrage du service pendant un wifi-apply : l'échéance d'origine est reprise (même boot_id, borne
        APPLY_DEADLINE_S) ; sinon (autre démarrage, identité inconnue, fichier invalide) le job est abandonné."""
        try:
            with open(self.cfg.apply_state, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            d = None
        job = d.get("job") if isinstance(d, dict) else None
        dl = d.get("deadline") if isinstance(d, dict) else None
        if self.setup_mode and isinstance(job, str) and netjobs.ID_RE.fullmatch(job) and self.boot_id \
                and d.get("boot_id") == self.boot_id and isinstance(dl, (int, float)) and not isinstance(dl, bool) \
                and 0 < dl - self.mono() <= APPLY_DEADLINE_S:
            self.apply = {"job": job, "deadline": float(dl)}
            self.pending[job] = "wifi-apply"
            self.phase, self.phase_msg = "applying", "Connexion au Wi-Fi en cours…"
            self.log("apply_resumed")
            return
        if isinstance(job, str) and netjobs.ID_RE.fullmatch(job):
            self._mark_stale(job)
            netjobs.withdraw(self.cfg.jobs_dir, job)
        self.apply = None
        self._apply_save()
        if self.setup_mode:
            self.phase, self.phase_msg = "error", ERR_FR["apply_timeout"]
        self.log("apply_abandoned", reason="restart")

    def _mark_stale(self, jid):
        self.stale_jobs.append(jid)
        del self.stale_jobs[:-STALE_JOBS_MAX]

    def _apply_deadline(self):
        if self.apply is None or self.mono() < self.apply["deadline"]:
            return
        jid = self.apply["job"]
        self.pending.pop(jid, None)
        self._mark_stale(jid)
        withdrawn = netjobs.withdraw(self.cfg.jobs_dir, jid)   # encore en file : le helper ne l'exécutera jamais
        self.apply = None
        self._apply_save()
        self.log("apply_timeout", withdrawn=withdrawn, seconds=int(APPLY_DEADLINE_S))
        if self.setup_mode:
            # échec honnête + nouveau QR ; le hotspot ne repart que selon _hotspot() (budget non réarmé)
            self.phase, self.phase_msg = "error", ERR_FR["apply_timeout"]
            self.token.issue()

    # -- boucle (toutes les ~2 s)
    def tick(self):
        net = netstate.snapshot(self.runner)
        results = netjobs.read_results(self.cfg.results_dir)
        with self.lock:
            self.net = net
            if net["mode"] == "offline":
                if self.offline_since is None:
                    self.offline_since = self.mono()
            else:
                self.offline_since = None
            for r in results:
                self._on_result(r)
            self._apply_deadline()
            if not self.setup_mode and os.path.exists(self.cfg.setup_flag):
                self.setup_mode, self.phase, self.phase_msg = True, "waiting", None
                self.token.issue()
            if self.setup_mode:
                if self.phase != "applying" and self.token.needs_rotation():
                    self.token.issue()
            self._hotspot_guard()
            if self.setup_mode:
                self._hotspot()
            if self.message and self.mono() >= self.message["until"]:
                self.message = None
            prime = self.setup_mode and net["mode"] == "offline" and net["wifi_present"] and \
                not net.get("ap_active") and self.ap is None
        self._cec_refresh()
        if prime:
            self._scan_prime()

    def _scan_prime(self):
        """Hors ligne, avant le Wi-Fi temporaire (qui rend wlan0 aveugle aux autres réseaux) : relève la liste des
        réseaux proches dans un thread, pour la proposer ensuite au téléphone connecté au point d'accès. Jamais dans
        la boucle (watchdog) ; au plus un relevé en cours et un par SCAN_PRIME_S."""
        if self.mono() - self.scan_prime_at < SCAN_PRIME_S:
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self.scan_prime_at = self.mono()
        self._scan_thread = threading.Thread(target=self.wifi_scan.networks, daemon=True, name="wifi-scan")
        self._scan_thread.start()

    def _cec_refresh(self):
        """Découverte CEC hors de la boucle (thread) : un cec-ctl lent ne retarde jamais le watchdog."""
        if self.mono() - self.cec_at < CEC_REFRESH_S:
            return
        if self._cec_thread is not None and self._cec_thread.is_alive():
            return
        self.cec_at = self.mono()

        def work():
            try:
                res = cec.discover(self.cec_root, runner=self.runner)
            except OSError:
                res = {"adapters": [], "discovery_only": True}
            with self.lock:
                self.cec = res
        self._cec_thread = threading.Thread(target=work, daemon=True, name="cec-discovery")
        self._cec_thread.start()

    def _submit(self, action, payload):
        jid = netjobs.submit(self.cfg.jobs_dir, self.cfg.jobs_tmp, action, payload)
        self.pending[jid] = action
        self.log("net_job_submitted", action=action)
        return jid

    def _on_result(self, r):
        action = self.pending.pop(r["id"], None)
        if action is None or action != r["action"]:
            # job abandonné à l'échéance, d'une exécution précédente ou inconnu : jamais d'effet sur l'état
            self.log("net_job_result_ignored", action=r["action"], ok=r["ok"],
                     reason="stale" if r["id"] in self.stale_jobs else "unknown")
            return
        self.log("net_job_result", action=action, ok=r["ok"], error=r["error"])
        if action == "ap-start":
            if self.ap is not None and self.ap.get("job") == r["id"]:
                self.ap["state"] = "active" if r["ok"] else "failed"
            if not r["ok"]:
                self.ap_retry_at = self.mono() + AP_RETRY_S
                self.phase_msg = ERR_FR["ap_start_failed"]
        elif action == "wifi-apply":
            self._apply_clear()
            if r["ok"]:
                self.ap = None
                self._finish_setup()
            else:
                self.phase, self.phase_msg = "error", ERR_FR.get(r["error"], ERR_FR["wifi_connect_failed"])
                self.token.issue()
        elif action == "ap-stop":
            self.ap = None

    def _hotspot(self):
        mode = self.net["mode"]
        if mode == "hotspot":
            if self.ap is not None:
                self.ap["state"] = "active"
            return
        if mode != "offline" or self.phase == "applying" or self.hs["expired"]:
            return
        if not self.cfg.hotspot_enabled or not self.net["wifi_present"]:
            return
        if self.ap is not None and self.ap["state"] == "starting":
            return
        if "ap-start" in self.pending.values() or self.mono() < self.ap_retry_at:
            return
        if self.offline_since is None or self.mono() - self.offline_since < self.cfg.hotspot_grace_s:
            return
        if self.hs["first_start"] is None:
            self.hs["first_start"] = self.mono()
            self._hs_save()
        ssid = "OpenTV-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
        psk = "".join(secrets.choice(AP_PSK_ALPHABET) for _ in range(12))
        jid = self._submit("ap-start", {"ssid": ssid, "psk": psk})
        self.ap = {"ssid": ssid, "psk": psk, "state": "starting", "job": jid}

    # -- page de configuration (LAN)
    def want_lan(self):
        with self.lock:
            return self.setup_mode and self.net["mode"] != "offline"

    def setup_context(self):
        with self.lock:
            return {"net_mode": self.net["mode"], "name": (self.device or {}).get("name") or "",
                    "policy": self.registry.policy()}

    def check_token(self, tok):
        """Affichage du formulaire (GET) : lecture seule, n'use jamais les essais (seule une soumission POST compte),
        sinon n'importe quel appareil du LAN pourrait brûler le QR en rafraîchissant une mauvaise adresse."""
        with self.lock:
            return self.setup_mode and self.phase != "applying" and self.token.check(tok)

    def setup_networks(self, tok, client=None):
        """Réseaux Wi-Fi proches pour l'étape 1 de l'assistant -> liste, ou None (jeton refusé / adresse limitée).

        Même contrôle que l'affichage du formulaire (jamais d'essai consommé). nmcli n'est lancé qu'avec un jeton
        valide, hors du verrou du service (la boucle n'attend jamais un balayage), et son coût est borné par le cache
        du Scanner quel que soit le nombre de requêtes."""
        with self.lock:
            if self._bad_token_limited(client, record=False):
                return None
            if not (self.setup_mode and self.phase != "applying" and self.token.check(tok)):
                return None
            hotspot = self.net["mode"] == "hotspot"
        return self.wifi_scan.networks(allow_stale=hotspot)

    def _bad_token_limited(self, client, record):
        """Débit des POST à jeton faux par adresse IP (table bornée). -> True si l'adresse est limitée."""
        now, ip = self.mono(), client if isinstance(client, str) else "?"
        win = [t for t in self.bad_token_ips.get(ip, ()) if t > now - BAD_TOKEN_WINDOW_S]
        if record:
            win.append(now)
        if win:
            self.bad_token_ips[ip] = win[-BAD_TOKEN_PER_IP:]
        else:
            self.bad_token_ips.pop(ip, None)
        if len(self.bad_token_ips) > BAD_TOKEN_TRACKED_IPS:
            for k in [k for k, v in self.bad_token_ips.items() if v[-1] <= now - BAD_TOKEN_WINDOW_S]:
                del self.bad_token_ips[k]
            while len(self.bad_token_ips) > BAD_TOKEN_TRACKED_IPS:
                del self.bad_token_ips[min(self.bad_token_ips, key=lambda k: self.bad_token_ips[k][-1])]
        return len(win) >= BAD_TOKEN_PER_IP

    def setup_submit(self, form, client=None):
        """-> (code HTTP, page, message d'erreur). Le jeton n'est consommé que par une soumission valide.

        Un jeton faux (ou une soumission pendant « applying ») ne compte jamais comme essai : seul le formulaire
        invalide accompagné du **bon** jeton use le QR (5 essais). Les jetons faux sont limités par IP (429)."""
        g = lambda k: form.get(k) if isinstance(form.get(k), str) else ""   # noqa: E731
        with self.lock:
            if self._bad_token_limited(client, record=False):
                return 429, "denied", None
            if not self.setup_mode or self.phase == "applying":
                return 403, "denied", None
            if not self.token.check(g("t")):
                self._bad_token_limited(client, record=True)
                return 403, "denied", None
            err, payload = None, None
            try:
                name = validate.device_name(g("name"))
            except ValueError:
                err = ERR_FORM_NAME
            net = g("net")
            if err is None and net not in ("keep", "wifi"):
                err = ERR_FORM_NET
            if err is None and net == "keep" and self.net["mode"] not in ("ethernet", "wifi"):
                err = ERR_FORM_OFFLINE
            if err is None and net == "wifi":
                try:
                    ssid = validate.ssid(g("ssid"))
                except ValueError:
                    err = ERR_FORM_SSID
                if err is None:
                    try:
                        psk = validate.psk(g("psk"))
                    except ValueError:
                        err = ERR_FORM_PSK
                if err is None:
                    restore = self.net["mode"] == "hotspot"
                    payload = {"ssid": ssid, "psk": psk, "hidden": g("hidden") == "1",
                               "delay_s": 3 if restore else 0, "restore_ap": restore}
            policy = {}   # chaque agent connu est enregistré, même « aucun accès » (scopes vides = refus)
            for a in KNOWN_AGENTS:
                lvl = g("agent_" + a) or "none"
                if lvl not in AGENT_LEVELS:
                    err = err or ERR_FORM_AGENT
                else:
                    policy[a] = list(AGENT_LEVELS[lvl])
            if err is not None:
                self.token.fail_attempt()
                return 400, "form", err
            if not self.token.consume(g("t")):
                return 403, "denied", None
            if payload is not None:
                # Wi-Fi non encore confirmé : la configuration reste ouverte même après un redémarrage du service
                self._touch_setup_flag()
            config.save_device(self.cfg, {"name": name})
            self.device = config.load_device(self.cfg)
            self.registry.set_policy(policy)
            for a, info in self.registry.list().items():   # droits réduits au QR : l'ancien accès est révoqué
                if not set(info["scopes"]) <= set(policy.get(a, [])):
                    self.registry.revoke(a)
            self.log("setup_submitted", network=net, agents=sum(1 for v in policy.values() if v))
            if payload is not None:
                jid = self._submit("wifi-apply", payload)
                payload = None
                self.apply = {"job": jid, "deadline": self.mono() + APPLY_DEADLINE_S}
                self._apply_save()
                self.phase, self.phase_msg = "applying", "Connexion au Wi-Fi en cours…"
                return 200, "applying", None
            self._finish_setup()
            return 200, "done", None

    # -- vue TV (loopback)
    def _setup_view(self):
        if not self.setup_mode:
            return {"active": False, "phase": self.phase, "message": None, "url": None, "token": None,
                    "expires_in": None, "hotspot": None}
        mode, ip = self.net["mode"], self.net["ip"]
        cur = self.token.current() if self.phase != "applying" else None
        url = None
        port = self.lan_port_actual or self.cfg.setup_port
        if cur and mode != "offline" and ip:
            url = "http://%s:%d/setup?t=%s" % (ip, port, cur[0])
        hs = None
        if self.ap is not None and self.ap["state"] in ("starting", "active"):
            hs = {"ssid": self.ap["ssid"], "state": self.ap["state"],
                  "wifi_qr": "WIFI:T:WPA;S:%s;P:%s;;" % (self.ap["ssid"], self.ap["psk"])}
        msg = self.phase_msg
        if mode == "offline" and not hs and not self.boot_id and self.cfg.hotspot_enabled:
            msg = MSG_BOOT_UNKNOWN
        elif mode == "offline" and self.hs["expired"] and not hs:
            msg = MSG_HOTSPOT_EXPIRED
        elif msg is None and mode == "offline":
            if hs:
                msg = "Création du Wi-Fi temporaire OpenTV…"
            else:
                msg = MSG_NO_NETWORK if (self.cfg.hotspot_enabled and self.net["wifi_present"]) else MSG_NO_NETWORK_NO_AP
        return {"active": True, "phase": self.phase, "message": msg, "url": url,
                "token": cur[0] if cur else None, "expires_in": cur[1] if cur else None, "hotspot": hs}

    def tv_state(self):
        with self.lock:
            m = self.message
            if m and self.mono() >= m["until"]:
                m = self.message = None
            return {"device": (self.device or {}).get("name") or "OpenTV", "configured": self.device is not None,
                    "version": VERSION, "network": {"mode": self.net["mode"], "ip": self.net["ip"]},
                    "setup": self._setup_view(),
                    "message": {"text": m["text"], "from": m["from"],
                                "seconds_left": int(m["until"] - self.mono())} if m else None,
                    "agents": sorted(self.registry.list()), "cec": {"adapters": len(self.cec["adapters"])},
                    "foryou": self.foryou is not None}

    def tv_foryou(self):
        if self.foryou is None:
            return {"rows": [], "note": "« Pour toi » est indisponible sur cette boîte.", "marks": {}, "buttons": []}
        with self.fy_lock:
            if self.fy_session is None or self.mono() - self.fy_at >= FORYOU_TTL_S:
                self.fy_session, self.fy_at = self.foryou.tv_session(), self.mono()
            return self.fy_session["data"]

    def tv_feedback(self, uid, action):
        if self.foryou is None:
            return None, "« Pour toi » est indisponible."
        with self.fy_lock:
            if self.fy_session is None:
                self.fy_session, self.fy_at = self.foryou.tv_session(), self.mono()
            return self.foryou.tv_feedback(self.fy_session, uid, action)

    # -- contexte des outils agents
    def agent_status(self, agent, scopes):
        with self.lock:
            mode = self.net["mode"]
            return {"device": (self.device or {}).get("name") or "OpenTV", "configured": self.device is not None,
                    "setup_active": self.setup_mode, "network": {"mode": mode, "online": mode in ("ethernet", "wifi")},
                    "cec": {"adapters": len(self.cec["adapters"]), "discovery_only": True},
                    "agent": {"id": agent, "scopes": list(scopes)}, "relay": {"status": "not_implemented"},
                    "version": VERSION}

    def show_message(self, agent, text, seconds):
        with self.lock:
            self.message = {"text": text, "from": agent, "until": self.mono() + seconds}
        self.log("agent_message", agent=agent, length=len(text))
        return {"shown": True, "seconds": seconds}
