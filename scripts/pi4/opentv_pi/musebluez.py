"""Compatibilité BlueZ exigée par le SDK Muse épinglé (install.sh du commit 1db6a26), appliquée de façon réversible.

Le SDK officiel règle deux choses sur la machine (linux/install.sh, configure_bluez / disable_bluez_battery) :
- `[GATT] ExchangeMTU = 256` dans /etc/bluetooth/main.conf : l'app Muse Android écrit des paquets de (MTU - 3) octets
  et échoue au-delà de 512 ; une valeur ≤ 256 déjà active est conservée telle quelle ;
- greffon `battery` de bluetoothd désactivé (`--noplugin=battery` ajouté à la commande effective de bluetooth.service
  par un drop-in) : sans cela BlueZ demande à l'iPhone de se lier (bond), ce que le gadget refuse, et l'appariement
  iOS peut tomber. Android et iPhone sont donc tous deux concernés.

Ici, chaque changement est POSSÉDÉ et réversible :
- main.conf : une ligne marquée `# opentv-muse: …` puis `ExchangeMTU = 256` insérée dans [GATT] (après la ligne
  commentée d'origine s'il y en a une, sinon en tête de section ; section ajoutée en fin de fichier si absente). Une
  valeur active > 256 posée localement n'est JAMAIS remplacée par l'installation : seul `opentv-pi muse
  prepare-bluetooth --replace-mtu` (geste explicite) la remplace, ligne d'origine notée et rendue à la désinstallation ;
- drop-in /etc/systemd/system/bluetooth.service.d/zz-opentv-muse.conf (dernier par ordre lexical) calculé depuis la
  commande effective SANS lui ; un fichier étranger à cet emplacement n'est jamais écrasé ; une commande complexe
  (guillemets, variables, continuation) n'est jamais réécrite ;
- état noté dans /var/lib/opentv-install/muse-bluez.json (empreintes, mode, ligne d'origine) + copie exacte de
  main.conf d'avant ; écriture atomique, notée AVANT de modifier ; un échec en cours rend les octets d'avant.
- `revert` (uninstall.sh, --purge compris) : fichier inchangé depuis ⇒ octets d'origine exacts ; modifié depuis ⇒
  seules les lignes OpenTV sont retirées/rendues ; drop-in retiré seulement s'il est encore identique au nôtre.
- prêt (`status`) = configuration compatible ET bluetoothd en cours d'exécution avec --noplugin=battery, démarré
  APRÈS la dernière modification de main.conf ; sinon l'appariement est refusé avec la commande exacte à lancer.
Bluetooth n'est redémarré que si OpenTV a changé quelque chose (ou sur demande explicite), jamais en dry-run ni dans
une racine de test.
"""
import hashlib
import json
import os
import re
import stat

from . import fsutil

MAIN_CONF = "/etc/bluetooth/main.conf"
DROPIN_DIR = "/etc/systemd/system/bluetooth.service.d"
DROPIN = DROPIN_DIR + "/zz-opentv-muse.conf"
RECORD = "/var/lib/opentv-install/muse-bluez.json"
MAIN_ORIG = "/var/lib/opentv-install/muse-bluez.main.conf.orig"
UNIT = "bluetooth.service"
UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system", "/usr/local/lib/systemd/system", "/usr/lib/systemd/system",
             "/lib/systemd/system")
MTU = 256
MTU_MIN = 23
MARKER = "# opentv-muse: ligne suivante ajoutée par OpenTV (pont Muse), retirée par /opt/opentv/uninstall.sh"
MANAGED_LINE = "ExchangeMTU = %d" % MTU
DROPIN_HEAD = ("# opentv-muse: géré par OpenTV (pont Muse, SDK officiel 1db6a26), retiré par\n"
               "# /opt/opentv/uninstall.sh.\n"
               "# Greffon battery de BlueZ désactivé : sinon BlueZ demande à l'iPhone de se lier, ce que le gadget\n"
               "# refuse, et l'appariement iOS peut tomber. Ne pas modifier : régénéré par\n"
               "# opentv-pi muse prepare-bluetooth.\n")
MAX_CONF = 256 * 1024
_SECTION_RE = re.compile(r"^\s*\[([^\]]*)\]\s*$")
_MTU_RE = re.compile(r"^\s*ExchangeMTU\s*=\s*(.*?)\s*$")
_MTU_COMMENT_RE = re.compile(r"^\s*#\s*ExchangeMTU\s*=.*$")
_UNSAFE_CMD = set("\"'\\$%;`")


class BluezError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class BlueZ:
    """Racine (« / » ou racine de test) et lecteur systemctl injectés ; aucune commande lancée hors `restart`."""

    def __init__(self, root="/", runner=None, proc_root=None):
        self.root, self.runner = root, runner
        self.proc_root = proc_root or self.p("/proc")

    def p(self, path):
        return path if self.root == "/" else os.path.join(self.root, path.lstrip("/"))

    # ------------------------------------------------------------------ lectures sûres
    def _read(self, path, cap=MAX_CONF):
        """-> bytes | None (absent) ; BluezError si lien, non régulier, trop grand."""
        q = self.p(path)
        try:
            st = os.lstat(q)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise BluezError("unexpected_file:" + path)
        if st.st_size > cap:
            raise BluezError("too_large:" + path)
        return fsutil.read_regular(q, cap + 1)

    def _text(self, path):
        raw = self._read(path)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BluezError("not_utf8:" + path)

    def record(self):
        raw = self._read(RECORD, 64 * 1024)
        if raw is None:
            return {}
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise BluezError("record_unreadable")
        if not isinstance(d, dict) or d.get("v") != 1:
            raise BluezError("record_unreadable")
        return d

    # ------------------------------------------------------------------ main.conf
    @staticmethod
    def _lines(text):
        return text.splitlines(True)

    @staticmethod
    def scan_mtu(text):
        """-> {"state": absent|ok|too_high|invalid|ambiguous, "value", "line", "gatt", "comment", "managed"} où line,
        gatt, comment et managed sont des indices de ligne (dernière section [GATT], ligne commentée, notre
        marqueur)."""
        lines = (text or "").splitlines()
        sec, gatt, active, comment, managed = None, None, [], None, None
        for i, l in enumerate(lines):
            m = _SECTION_RE.match(l)
            if m:
                sec = m.group(1).strip()
                if sec == "GATT":
                    gatt = i
                continue
            if l.strip() == MARKER and i + 1 < len(lines) and lines[i + 1].strip() == MANAGED_LINE:
                managed = i
            if sec != "GATT":
                continue
            if _MTU_RE.match(l):
                active.append(i)
            elif _MTU_COMMENT_RE.match(l) and comment is None:
                comment = i
        out = {"state": "absent", "value": None, "line": None, "gatt": gatt, "comment": comment, "managed": managed}
        if len(active) > 1:
            out["state"] = "ambiguous"
            return out
        if active:
            raw = _MTU_RE.match(lines[active[0]]).group(1)
            out["line"] = active[0]
            if re.fullmatch(r"[0-9]{1,5}", raw):
                out["value"] = int(raw)
                v = out["value"]
                out["state"] = "ok" if MTU_MIN <= v <= MTU else "too_high" if v > MTU else "invalid"
            else:
                out["state"] = "invalid"
        return out

    @staticmethod
    def _eol(text):
        return "\r\n" if "\r\n" in (text or "") else "\n"

    def mtu_plan(self, text, replace=False):
        """-> (nouveau texte | None si rien à faire, mode, ligne d'origine) ; BluezError si refus."""
        sc = self.scan_mtu(text)
        if sc["state"] == "ok":
            return None, None, None
        if sc["state"] == "ambiguous":
            raise BluezError("mtu_ambiguous")
        nl = self._eol(text)
        lines = self._lines(text or "")
        block = [MARKER + nl, MANAGED_LINE + nl]
        if sc["state"] in ("too_high", "invalid"):
            if not replace:
                raise BluezError("mtu_local_value")
            i = sc["line"]
            orig = lines[i].rstrip("\r\n")
            end = lines[i][len(orig):] or nl
            lines[i:i + 1] = [MARKER + nl, MANAGED_LINE + end]
            return "".join(lines), "replace", orig
        if sc["gatt"] is not None:
            at = (sc["comment"] if sc["comment"] is not None else sc["gatt"]) + 1
            if at > 0 and not lines[at - 1].endswith(("\n", "\r")):
                lines[at - 1] += nl
            lines[at:at] = block
            return "".join(lines), "insert", None
        prefix = text or ""
        if prefix and not prefix.endswith("\n"):
            prefix += nl
        if prefix:
            prefix += nl
        return prefix + "[GATT]" + nl + "".join(block), "append_section", None

    @staticmethod
    def mtu_remove(text, rec):
        """Retrait chirurgical (fichier modifié depuis) : notre marqueur + ligne gérée retirés, ligne d'origine rendue
        en mode replace, section [GATT] ajoutée retirée si elle est restée vide. -> texte | None (rien trouvé)."""
        lines = text.splitlines(True)
        for i, l in enumerate(lines):
            if l.strip() == MARKER and i + 1 < len(lines) and lines[i + 1].strip() == MANAGED_LINE:
                end = lines[i + 1][len(lines[i + 1].rstrip("\r\n")):]
                if rec.get("mode") == "replace" and isinstance(rec.get("original_line"), str):
                    lines[i:i + 2] = [rec["original_line"] + end]
                else:
                    del lines[i:i + 2]
                    if rec.get("mode") == "append_section" and i > 0 and lines[i - 1].strip() == "[GATT]" and \
                            (i == len(lines) or _SECTION_RE.match(lines[i])):   # section ajoutée restée vide
                        j = i - 1
                        del lines[j]
                        if j > 0 and not lines[j - 1].strip():
                            del lines[j - 1]
                return "".join(lines)
        return None

    # ------------------------------------------------------------------ bluetooth.service
    def _unit_layers(self, exclude_ours=False):
        """-> (main | None, [drop-ins]) en ordre d'application systemd ; chaque élément (chemin, texte, masqué)."""
        main = None
        for d in UNIT_DIRS:
            q = self.p(d + "/" + UNIT)
            if os.path.islink(q):
                target = os.readlink(q)
                if target == "/dev/null":
                    return ("masked", "", True), []
                if os.path.isabs(target):
                    q = self.p(target)
                else:
                    q = os.path.join(os.path.dirname(q), target)
                if not os.path.isfile(q) or os.path.islink(q):
                    continue
            elif not os.path.isfile(q):
                continue
            with open(q, "rb") as f:
                main = (d + "/" + UNIT, f.read(MAX_CONF).decode("utf-8", "replace"), False)
            break
        drops = {}
        for d in UNIT_DIRS:
            dd = self.p(d + "/" + UNIT + ".d")
            if not os.path.isdir(dd) or os.path.islink(dd):
                continue
            for name in sorted(os.listdir(dd)):
                if not name.endswith(".conf") or name in drops:
                    continue
                path = d + "/" + UNIT + ".d/" + name
                if exclude_ours and path == DROPIN:
                    drops[name] = None
                    continue
                q = os.path.join(dd, name)
                if os.path.islink(q) and os.readlink(q) == "/dev/null":
                    drops[name] = None
                    continue
                if not os.path.isfile(q):
                    continue
                with open(q, "rb") as f:
                    drops[name] = (path, f.read(MAX_CONF).decode("utf-8", "replace"), False)
        return main, [drops[n] for n in sorted(drops) if drops[n] is not None]

    @staticmethod
    def _execstarts(text):
        """Affectations ExecStart= de la section [Service] (« » = remise à zéro). None si continuation de ligne."""
        sec, out = None, []
        for l in text.splitlines():
            s = l.strip()
            m = _SECTION_RE.match(s)
            if m:
                sec = m.group(1).strip()
                continue
            if sec != "Service" or not s.startswith("ExecStart"):
                continue
            k, eq, v = s.partition("=")
            if not eq or k.strip() != "ExecStart":
                continue
            if v.rstrip().endswith("\\"):
                return None
            out.append(v.strip())
        return out

    def effective(self, exclude_ours=False):
        """-> {"state": ok|missing|masked|complex, "command": str|None, "from": chemin, "last_dropin": nom}."""
        try:
            main, drops = self._unit_layers(exclude_ours)
        except OSError:
            return {"state": "unreadable", "command": None}
        if main is None:
            return {"state": "missing", "command": None}
        if main[2]:
            return {"state": "masked", "command": None}
        cmds, src = [], None
        for path, text, _ in [main] + drops:
            ex = self._execstarts(text)
            if ex is None:
                return {"state": "complex", "command": None, "from": path}
            for v in ex:
                if v:
                    cmds.append(v)
                    src = path
                else:
                    cmds = []
        if len(cmds) != 1:
            return {"state": "complex" if cmds else "missing", "command": None, "from": src}
        cmd = cmds[0]
        if cmd[:1] in "@-:+!" or any(c in _UNSAFE_CMD for c in cmd):
            return {"state": "complex", "command": None, "from": src}
        return {"state": "ok", "command": cmd, "from": src,
                "last_dropin": os.path.basename(drops[-1][0]) if drops else None}

    @staticmethod
    def battery_disabled(args):
        """Arguments de bluetoothd -> True si battery est dans --noplugin/-P."""
        for i, a in enumerate(args):
            names = None
            if a.startswith("--noplugin="):
                names = a.split("=", 1)[1]
            elif a in ("-P", "--noplugin") and i + 1 < len(args):
                names = args[i + 1]
            elif a.startswith("-P") and len(a) > 2:
                names = a[2:]
            if names is not None and "battery" in names.split(","):
                return True
        return False

    @classmethod
    def with_battery_disabled(cls, cmd):
        args = cmd.split()
        if cls.battery_disabled(args):
            return cmd
        for i, a in enumerate(args):
            if a.startswith("--noplugin="):
                names = a.split("=", 1)[1]
                args[i] = "--noplugin=" + (names + ",battery" if names else "battery")
                return " ".join(args)
            if a in ("-P", "--noplugin") and i + 1 < len(args):
                args[i + 1] = args[i + 1] + ",battery"
                return " ".join(args)
            if a.startswith("-P") and len(a) > 2:
                args[i] = a + ",battery"
                return " ".join(args)
        return " ".join(args + ["--noplugin=battery"])

    def dropin_text(self, cmd):
        return DROPIN_HEAD + "[Service]\nExecStart=\nExecStart=" + cmd + "\n"

    def dropin_plan(self, rec):
        """-> (texte voulu | None si inutile, raison). BluezError si emplacement étranger ou commande illisible."""
        base = self.effective(exclude_ours=True)
        if base["state"] != "ok":
            raise BluezError("bluetooth_unit_" + base["state"])
        if self.battery_disabled(base["command"].split()):
            return None, "battery_disabled_locally"
        cur = self._read(DROPIN, 64 * 1024)
        if cur is not None and _sha(cur) != (rec.get("dropin") or {}).get("sha256"):
            raise BluezError("foreign_dropin")
        return self.dropin_text(self.with_battery_disabled(base["command"])), None

    # ------------------------------------------------------------------ bluetoothd en cours
    def _systemctl(self, *args):
        if self.runner is None:
            return 1, ""
        rc, out = self.runner(["systemctl"] + list(args), 15)
        return rc, out or ""

    def runtime(self):
        """-> {"running", "pid", "battery_disabled", "started_at"} ; None quand inconnu (racine de test)."""
        out = {"running": None, "pid": None, "battery_disabled": None, "started_at": None}
        rc, txt = self._systemctl("show", "-p", "MainPID", "--value", UNIT)
        m = re.fullmatch(r"\s*([0-9]{1,9})\s*", txt) if rc == 0 else None
        if not m:
            return out
        pid = int(m.group(1))
        if pid <= 0:
            out["running"] = False
            return out
        out["running"], out["pid"] = True, pid
        try:
            with open(os.path.join(self.proc_root, str(pid), "cmdline"), "rb") as f:
                args = [a.decode("utf-8", "replace") for a in f.read(65536).split(b"\0") if a]
            out["battery_disabled"] = self.battery_disabled(args[1:])
        except OSError:
            pass
        try:
            with open(os.path.join(self.proc_root, str(pid), "stat"), "rb") as f:
                fields = f.read(4096).decode("ascii", "replace").rsplit(")", 1)[1].split()
            with open(os.path.join(self.proc_root, "stat"), "rb") as f:
                btime = int(re.search(rb"^btime\s+([0-9]+)", f.read(65536), re.M).group(1))
            out["started_at"] = btime + int(fields[19]) / float(os.sysconf("SC_CLK_TCK"))
        except (OSError, IndexError, ValueError, AttributeError):
            pass
        return out

    # ------------------------------------------------------------------ état
    def status(self):
        rep = {"ready": False, "mtu": None, "battery": None, "runtime": None, "gate": None, "note": None}
        try:
            text = self._text(MAIN_CONF)
            rec = self.record()
        except (BluezError, OSError) as e:
            rep.update(gate="bluetooth_config_unreadable", error=getattr(e, "code", "unreadable"),
                       note="Vérifier /etc/bluetooth/main.conf et %s (fichiers réguliers, pas de lien)." % RECORD)
            return rep
        sc = self.scan_mtu(text)
        rep["mtu"] = {"state": sc["state"], "value": sc["value"],
                      "managed": sc["managed"] is not None and (rec.get("mtu") or {}).get("mode") is not None}
        eff = self.effective()
        bat = {"state": eff["state"]}
        if eff["state"] == "ok":
            disabled = self.battery_disabled(eff["command"].split())
            ours = eff.get("from") == DROPIN
            bat["state"] = ("disabled_managed" if ours else "disabled_local") if disabled else "enabled"
            if not disabled and os.path.isfile(self.p(DROPIN)) and eff.get("from") != DROPIN:
                bat["state"] = "overridden"
            if not disabled:
                try:
                    cur = self._read(DROPIN, 64 * 1024)
                except (BluezError, OSError):
                    cur = b""
                if cur is not None and _sha(cur) != (rec.get("dropin") or {}).get("sha256"):
                    bat["state"] = "foreign_dropin"
        rep["battery"] = bat
        rt = self.runtime()
        rep["runtime"] = rt
        config_ok = sc["state"] == "ok" and bat["state"] in ("disabled_managed", "disabled_local")
        if not config_ok:
            rep["gate"] = "bluetooth_not_prepared"
            if sc["state"] in ("too_high", "invalid"):
                rep["note"] = ("ExchangeMTU = %s réglé localement dans /etc/bluetooth/main.conf (Android échoue "
                               "au-delà de 256) : sudo /opt/opentv/bin/opentv-pi muse prepare-bluetooth --replace-mtu "
                               "(ligne d'origine notée, rendue à la désinstallation)" % (sc["value"],))
            elif sc["state"] == "ambiguous":
                rep["note"] = ("Plusieurs ExchangeMTU actifs dans [GATT] de /etc/bluetooth/main.conf : n'en garder "
                               "qu'un.")
            elif bat["state"] == "foreign_dropin":
                rep["note"] = ("%s existe et n'a pas été écrit par OpenTV : jamais écrasé. Le retirer ou y ajouter "
                               "--noplugin=battery, puis relancer." % DROPIN)
            elif bat["state"] in ("complex", "missing", "masked", "unreadable", "overridden"):
                rep["note"] = ("Commande de bluetooth.service illisible ou remplacée (%s) : ajouter --noplugin=battery "
                               "à bluetoothd manuellement (iPhone), puis relancer." % bat["state"])
            else:
                rep["note"] = "sudo /opt/opentv/bin/opentv-pi muse prepare-bluetooth"
            return rep
        if rt["running"] is None or (rt["running"] and (rt["battery_disabled"] is None or rt["started_at"] is None)):
            rep["gate"] = "bluetooth_runtime_unknown"
            rep["note"] = "État de bluetoothd illisible : sudo /opt/opentv/bin/opentv-pi muse prepare-bluetooth"
            return rep
        if not rt["running"]:
            rep["gate"] = "bluetooth_not_running"
            rep["note"] = "sudo systemctl enable --now bluetooth.service"
            return rep
        mtime = None
        try:
            mtime = os.stat(self.p(MAIN_CONF)).st_mtime if text is not None else None
        except OSError:
            pass
        stale = mtime is not None and rt["started_at"] + 1 < mtime
        if not rt["battery_disabled"] or stale:
            rep["gate"] = "bluetooth_restart_required"
            rep["note"] = ("Configuration prête mais bluetoothd pas encore redémarré : sudo /opt/opentv/bin/opentv-pi "
                           "muse prepare-bluetooth")
            return rep
        rep["ready"] = True
        return rep

    # ------------------------------------------------------------------ application (transactionnelle)
    def _write(self, path, data, like=None):
        q = self.p(path)
        mode, uid, gid = 0o644, None, None
        if like is not None:
            mode, uid, gid = stat.S_IMODE(like.st_mode), like.st_uid, like.st_gid
            if uid == os.geteuid() and gid == os.getegid():
                uid = gid = None
        fsutil.atomic_write_path(q, data, mode=mode, uid=uid, gid=gid)

    def _backup(self, path, backup_dir, saved):
        """Copie exacte (mode compris) dans backup_dir + chemin, comme backup() d'install.sh."""
        if not backup_dir:
            return
        src = self.p(path)
        dst = self.p(backup_dir.rstrip("/") + path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f:
            data = f.read()
        st = os.lstat(src)
        fsutil.atomic_write_path(dst, data, mode=stat.S_IMODE(st.st_mode))
        saved.append(path)

    def apply(self, replace_mtu=False, backup_dir=None, dry_run=False, installer=False):
        """Rend la configuration compatible. -> rapport {"changed": bool, "saved": [...], "created": [...],
        "dirs": [...], "skipped": [...], "plan": [...]} ; BluezError(code) si refus sans rien modifier (hors mode
        installer : refus non bloquants rapportés dans skipped). Toute erreur d'écriture rend les octets d'avant."""
        rep = {"changed": False, "saved": [], "created": [], "dirs": [], "skipped": [], "plan": []}
        try:
            rec = self.record()
            text = self._text(MAIN_CONF)
        except BluezError as e:   # lien, fichier spécial, trop grand, non UTF-8 : jamais modifié
            if not installer:
                raise
            rep["skipped"].append(e.code)
            return rep
        if text is None and not os.path.isdir(self.p(os.path.dirname(MAIN_CONF))):
            if installer:
                rep["skipped"].append("bluez_absent")
                return rep
            raise BluezError("bluez_absent")
        try:
            new_text, mode, orig_line = self.mtu_plan(text, replace=replace_mtu)
        except BluezError as e:
            if not installer:
                raise
            rep["skipped"].append(e.code)
            new_text = mode = orig_line = None
        try:
            dropin = self.dropin_plan(rec)[0]
        except BluezError as e:
            if not installer:
                raise
            rep["skipped"].append(e.code)
            dropin = None
        cur_drop = self._read(DROPIN, 64 * 1024)
        if dropin is not None and cur_drop is not None and cur_drop == dropin.encode("utf-8"):
            dropin = None   # déjà en place, identique
        if new_text is None and dropin is None:
            return rep
        if dry_run:
            if new_text is not None:
                rep["plan"].append("%s : [GATT] ExchangeMTU = %d (%s, noté dans %s)" % (MAIN_CONF, MTU, mode, RECORD))
            if dropin is not None:
                rep["plan"].append("%s : --noplugin=battery (greffon battery de BlueZ désactivé : iPhone)" % DROPIN)
            rep["changed"] = True
            return rep
        # 1) sauvegardes (rollback d'install.sh) puis état noté AVANT toute modification
        written = []   # (chemin, octets d'avant | None)
        try:
            new_rec = dict(rec, v=1)
            for path in (RECORD, MAIN_ORIG):
                if os.path.lexists(self.p(path)):
                    self._backup(path, backup_dir, rep["saved"])
            os.makedirs(self.p(os.path.dirname(RECORD)), exist_ok=True)
            if new_text is not None:
                raw = (text or "").encode("utf-8")
                if text is not None:
                    self._backup(MAIN_CONF, backup_dir, rep["saved"])
                if not (rec.get("mtu") or {}).get("mode"):
                    prev_orig = self._read(MAIN_ORIG, MAX_CONF)
                    written.append((MAIN_ORIG, prev_orig))
                    if text is not None:
                        self._write(MAIN_ORIG, raw)
                        if prev_orig is None:
                            rep["created"].append(MAIN_ORIG)
                new_rec["mtu"] = {"mode": mode, "original_line": orig_line, "orig_existed": text is not None,
                                  "after_sha256": _sha(new_text.encode("utf-8"))}
                if (rec.get("mtu") or {}).get("mode"):   # déjà géré (fichier modifié depuis) : origine conservée
                    new_rec["mtu"]["orig_existed"] = rec["mtu"].get("orig_existed", text is not None)
            if dropin is not None:
                created_dir = not os.path.isdir(self.p(DROPIN_DIR))
                new_rec["dropin"] = {"path": DROPIN, "sha256": _sha(dropin.encode("utf-8")),
                                     "dir_created": created_dir or bool((rec.get("dropin") or {}).get("dir_created"))}
            prev_rec = self._read(RECORD, 64 * 1024)
            written.append((RECORD, prev_rec))
            self._write(RECORD, (json.dumps(new_rec, sort_keys=True, indent=1) + "\n").encode("utf-8"))
            if prev_rec is None:
                rep["created"].append(RECORD)
            # 2) main.conf (atomique, mode/propriétaire conservés)
            if new_text is not None:
                like = os.lstat(self.p(MAIN_CONF)) if text is not None else None
                written.append((MAIN_CONF, (text or "").encode("utf-8") if text is not None else None))
                self._write(MAIN_CONF, new_text.encode("utf-8"), like=like)
                if text is None:
                    rep["created"].append(MAIN_CONF)
                rep["changed"] = True
            # 3) drop-in
            if dropin is not None:
                if not os.path.isdir(self.p(DROPIN_DIR)):
                    os.makedirs(self.p(DROPIN_DIR), mode=0o755)
                    rep["dirs"].append(DROPIN_DIR)
                written.append((DROPIN, cur_drop))
                if cur_drop is not None:
                    self._backup(DROPIN, backup_dir, rep["saved"])
                self._write(DROPIN, dropin.encode("utf-8"))
                if cur_drop is None:
                    rep["created"].append(DROPIN)
                rep["changed"] = True
                rep["dropin_changed"] = True
        except BaseException:
            for path, before in reversed(written):
                try:
                    if before is None:
                        os.unlink(self.p(path))
                    else:
                        self._write(path, before, like=os.lstat(self.p(path)) if os.path.exists(self.p(path)) else None)
                except OSError:
                    pass
            for d in rep["dirs"]:
                try:
                    os.rmdir(self.p(d))
                except OSError:
                    pass
            raise
        return rep

    # ------------------------------------------------------------------ retour arrière (désinstallation)
    def revert(self, dry_run=False):
        """Retire seulement ce qu'OpenTV a noté. -> {"changed": bool, "kept": [...], "plan": [...]}."""
        rep = {"changed": False, "kept": [], "plan": [], "dropin_changed": False}
        try:
            rec = self.record()
        except BluezError as e:   # marqueur illisible : rien deviné, rien retiré, la désinstallation continue
            rep["kept"].append(e.code)
            return rep
        if not rec:
            return rep
        m = rec.get("mtu") or {}
        if m.get("mode"):
            try:
                text = self._text(MAIN_CONF)
            except BluezError as e:   # remplacé localement par un lien/fichier spécial : jamais suivi ni modifié
                rep["kept"].append(e.code)
                text = None
            new = None
            if text is not None and _sha(text.encode("utf-8")) == m.get("after_sha256"):
                orig = self._read(MAIN_ORIG, MAX_CONF)
                if not m.get("orig_existed"):
                    new = b""
                elif orig is not None:
                    new = orig
            if new is None and text is not None:
                t = self.mtu_remove(text, m)
                if t is None:
                    rep["kept"].append("main_conf_modified_locally")
                else:
                    new = t.encode("utf-8")
            if new is not None:
                if dry_run:
                    rep["plan"].append("%s : ExchangeMTU d'OpenTV retiré (contenu d'origine rendu)" % MAIN_CONF)
                elif new == b"" and not m.get("orig_existed"):
                    os.unlink(self.p(MAIN_CONF))
                else:
                    self._write(MAIN_CONF, new, like=os.lstat(self.p(MAIN_CONF)))
                rep["changed"] = True
        dr = rec.get("dropin") or {}
        if dr.get("sha256"):
            try:
                cur = self._read(DROPIN, 64 * 1024)
            except BluezError as e:
                rep["kept"].append(e.code)
                cur = None
            if cur is not None and _sha(cur) == dr["sha256"]:
                if dry_run:
                    rep["plan"].append("%s retiré (greffon battery de BlueZ rétabli)" % DROPIN)
                else:
                    os.unlink(self.p(DROPIN))
                    if dr.get("dir_created"):
                        try:
                            os.rmdir(self.p(DROPIN_DIR))
                        except OSError:
                            rep["kept"].append("dropin_dir_not_empty")
                rep["changed"] = rep["dropin_changed"] = True
            elif cur is not None:
                rep["kept"].append("dropin_modified_locally")
        if not dry_run:
            for path in (MAIN_ORIG, RECORD):
                if os.path.lexists(self.p(path)):
                    os.unlink(self.p(path))
        return rep

    def restart(self, daemon_reload=False):
        """Redémarre bluetooth (seulement s'il tourne : try-restart). -> True si la commande a réussi."""
        if daemon_reload:
            self._systemctl("daemon-reload")
        rc, _ = self._systemctl("try-restart", UNIT)
        return rc == 0
