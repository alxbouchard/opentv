#!/bin/bash
# OpenTV Raspberry Pi 4 — désinstallation / retour arrière.
#
#   sudo /opt/opentv/uninstall.sh            arrête les services et la garde, ARRÊTE ET SUPPRIME le Wi-Fi temporaire
#                                            OpenTV, libère la CEC (Playback « OpenTV » effacé, touche OK du noyau
#                                            rétablie), retire les fichiers du manifeste (dont le drop-in logind de
#                                            la touche Power et la politique Chromium du kiosque), restaure les
#                                            fichiers d'origine sauvegardés, retire de cmdline.txt exactement les
#                                            jetons ajoutés (video=…, démarrage silencieux, splash Plymouth), retire
#                                            disable_splash=1 de config.txt (arc-en-ciel du firmware rétabli), rend
#                                            le thème Plymouth précédent et régénère l'initramfs (noyau v8 exact,
#                                            sauvegarde SHA-256 avant, octets d'avant rétablis si la régénération
#                                            échoue — arrêt alors AVANT tout retrait, pour relancer), retire l'écran OpenTV et
#                                            la condition getty de tty1, rend tty1, retire la promotion
#                                            terrain de « Regarder » (/etc/opentv/playback-promoted)
#   ... --purge                              efface aussi l'état (/var/lib/opentv*, /etc/opentv)
#   ... --purge --forget-wifi                oublie aussi le Wi-Fi configuré par OpenTV (opentv-wifi)
#   ... --dry-run | --root <dossier>         plan seul / rootfs de test
set -Eeuo pipefail

ROOT="/"
DRY=0
PURGE=0
FORGET_WIFI=0
die() { echo "opentv-uninstall: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || die "--root exige un chemin"; ROOT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --purge) PURGE=1; shift ;;
    --forget-wifi) FORGET_WIFI=1; shift ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "option inconnue : $1" ;;
  esac
done
case "$ROOT" in /*) ;; *) die "--root doit être un chemin absolu" ;; esac
if [ "$ROOT" != "/" ]; then ROOT="${ROOT%/}"; fi
LIVE=0
[ "$ROOT" = "/" ] && LIVE=1
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then die "exécuter en root (sudo) ou utiliser --dry-run"; fi

dest() { if [ "$ROOT" = "/" ]; then printf '%s' "$1"; else printf '%s%s' "$ROOT" "$1"; fi; }
act() { if [ "$DRY" = 1 ]; then say "[dry-run] $*"; else "$@"; fi; }
host() {
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then "$@" || say "(ignoré : $* a échoué)"
  elif [ "$DRY" = 1 ]; then say "[dry-run] $*"
  else say "[racine de test, non exécuté] $*"; fi
}

durable_sync() { sync -- "$@" 2>/dev/null || sync; }   # fsync des chemins nommés ; repli : sync global
# transaction initramfs partagée avec install.sh : installée (/opt/opentv/lib) ou arborescence source (system/)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IR_LIB=""
for c in "$HERE/lib/opentv-initramfs.sh" "$HERE/system/opentv-initramfs.sh"; do
  if [ -f "$c" ] && [ ! -L "$c" ]; then IR_LIB="$c"; break; fi
done
if [ -n "$IR_LIB" ]; then
  # shellcheck source-path=SCRIPTDIR source=system/opentv-initramfs.sh
  . "$IR_LIB"
  ir_init
fi
TS="$(date -u +%Y%m%dT%H%M%SZ)"
replace_durably() {   # temp synchronisé AVANT le renommage, cible + dossier APRÈS (jamais de cmdline.txt vide/tronqué)
  durable_sync "$1" || return 1
  mv -f -- "$1" "$2" || return 1
  durable_sync "$2" "$(dirname "$2")"
}

# 0) fichiers de démarrage EN PREMIER (cmdline.txt, config.txt, plymouthd.conf), puis initramfs. Chacun est d'abord
#    VÉRIFIÉ (partition montée, fichier présent et attendu) ; un seul refus ⇒ arrêt AVANT toute modification, marqueurs
#    et /opt/opentv/uninstall.sh conservés pour réessayer. Seul ce qu'OpenTV a noté est retiré (liste fermée) ; le
#    reste du fichier, même modifié depuis, et les fins de ligne sont conservés. Marqueurs effacés après écriture durable.
REC_DIR="$(dest /var/lib/opentv-install)"
VIDEO_REC="$REC_DIR/cmdline-video.added"
BOOT_REC="$REC_DIR/cmdline-boot.added"
CFG_REC="$REC_DIR/config-splash.added"
PLY_REC="$REC_DIR/plymouth-theme.added"
PLY_ORIG="$REC_DIR/plymouthd.conf.orig"
# initramfs régénéré par OpenTV (même si Theme=opentv était déjà choisi avant l'installation) : régénéré ici après
# restauration du thème, marqueur effacé seulement après une régénération vérifiée (sinon conservé pour un nouvel essai)
INITRD_REC="$REC_DIR/initramfs-plymouth.rebuilt"
INITRD_REC_LINE="rebuilt=opentv-plymouth"
MANIFEST="$(dest /opt/opentv/MANIFEST.sha256)"
DIRS_REC="$(dest /var/lib/opentv-install/created-dirs.list)"
CMDF="$(dest /boot/firmware/cmdline.txt)"
CFGF="$(dest /boot/firmware/config.txt)"
PLYC="$(dest /etc/plymouth/plymouthd.conf)"
INITRD="/boot/firmware/initramfs8"
hexof() { od -An -tx1 -v | tr -d ' \n'; }
tailhex() { tail -c "$2" -- "$1" | hexof; }
unhex() { local h="$1" o=""; while [ -n "$h" ]; do o="$o\\x${h:0:2}"; h="${h:2}"; done; printf '%b' "$o"; }
lines_upto() { if [ "$1" -gt 0 ]; then head -n "$1" -- "$2"; fi; }   # n premières lignes (n = 0 : rien)
file_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
safe_text() {   # fichier régulier (pas un lien), au plus <max> octets, sans octet nul
  local n z
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  n="$(wc -c < "$1" | tr -d ' ')" || return 1
  [ "$n" -le "$2" ] || return 1
  z="$(tr -d '\000' < "$1" | wc -c | tr -d ' ')" || return 1
  [ "$z" = "$n" ]
}
bounded() {
  local s="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then timeout -k 30 "$s" "$@"; else "$@"; fi
}
bootcmd() {   # initramfs : vraie racine exécutée ; racine de test : faux outils si OPENTV_INSTALL_TEST_BOOTCMDS=1
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then "$@"
  elif [ "$DRY" = 1 ]; then say "[dry-run] $*"
  elif [ "${OPENTV_INSTALL_TEST_BOOTCMDS:-}" = 1 ]; then OPENTV_TEST_ROOT="$ROOT" "$@"
  else say "[racine de test, non exécuté] $*"; fi
}
drop_arg() {   # drop_arg <ligne> <jeton> : ligne sans la dernière occurrence exacte du jeton
  local l=" $1 " pre post
  case "$l" in *" $2 "*) ;; *) printf '%s' "$1"; return 0 ;; esac
  pre="${l%" $2 "*}"
  post="${l##*" $2 "}"
  l="$pre $post"
  l="${l# }"
  printf '%s' "${l% }"
}
boot_blocked() {
  echo "opentv-uninstall: $*" >&2
  echo "opentv-uninstall: désinstallation NON effectuée, rien d'autre n'a été modifié ; marqueurs OpenTV conservés" \
    "dans /var/lib/opentv-install (cmdline-*.added, config-splash.added, plymouth-theme.added," \
    "initramfs-plymouth.rebuilt)." >&2
  echo "opentv-uninstall: corriger (ex. sudo mount /boot/firmware) puis relancer : sudo /opt/opentv/uninstall.sh" >&2
  exit 1
}
boot_mounted() {
  if [ "$LIVE" = 1 ] && command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q /boot/firmware; then
    boot_blocked "/boot/firmware n'est pas monté : $*"
  fi
}
# ply_scan <fichier> : dernière ligne Theme= de la dernière section [Daemon] (PLY_TLINE, valeur PLY_TVAL)
ply_scan() {
  local l n=0 sec=""
  PLY_TLINE=0; PLY_TVAL=""
  [ -f "$1" ] || return 0
  while IFS= read -r l || [ -n "$l" ]; do
    n=$((n + 1))
    l="${l%$'\r'}"
    l="${l#"${l%%[![:space:]]*}"}"
    case "$l" in
      '['*) sec="$l"; sec="${sec%"${sec##*[![:space:]]}"}" ;;
      Theme=*|Theme[[:space:]]*=*)
        if [ "$sec" = "[Daemon]" ]; then
          PLY_TLINE=$n
          PLY_TVAL="${l#*=}"; PLY_TVAL="${PLY_TVAL#"${PLY_TVAL%%[![:space:]]*}"}"
          PLY_TVAL="${PLY_TVAL%"${PLY_TVAL##*[![:space:]]}"}"
        fi ;;
    esac
  done < "$1"
}
# put_text <cible> <producteur…> : contenu produit dans un temp du même dossier (mode conservé), écriture durable
put_text() {
  local f="$1"
  shift
  "$@" > "$f.opentv-tmp" || return 1
  if [ -e "$f" ]; then chmod "$(file_mode "$f")" "$f.opentv-tmp" || return 1; fi
  replace_durably "$f.opentv-tmp" "$f"
}

# --- 0a) relevé (aucune écriture) : jetons cmdline notés (liste fermée)
DROP=()
for rec in "$VIDEO_REC" "$BOOT_REC"; do
  [ -f "$rec" ] || continue
  while IFS= read -r a || [ -n "$a" ]; do
    case "$a" in
      video=HDMI-A-1:1920x1080@60|quiet|loglevel=3|logo.nologo|vt.global_cursor_default=0|systemd.show_status=false|splash|plymouth.ignore-serial-consoles)
        DROP+=("$a") ;;
      *) [ -z "$a" ] || say "ignoré (jeton hors liste OpenTV) : $a" ;;
    esac
  done < "$rec"
done
# config.txt : suffixe exact noté par install.sh ; seules les 8 formes OpenTV sont acceptées (liste fermée)
CFG_SFX=""
if [ -f "$CFG_REC" ]; then
  while IFS= read -r a || [ -n "$a" ]; do case "$a" in suffix=*) CFG_SFX="${a#suffix=}" ;; esac; done < "$CFG_REC"
  ok=0
  for le in 0a 0d0a; do
    for pre in "" "$le"; do
      for hdr in "" "5b616c6c5d$le"; do
        [ "$CFG_SFX" = "$pre${hdr}64697361626c655f73706c6173683d31$le" ] && ok=1
      done
    done
  done
  if [ "$ok" = 0 ]; then say "ignoré (marqueur config.txt hors forme OpenTV)"; CFG_SFX=""; fi
fi
# plymouthd.conf : mode, ligne Theme= précédente (hex), empreinte du fichier écrit par install.sh
PLY_MODE=""
PLY_PREV=""
PLY_SHA=""
if [ -f "$PLY_REC" ]; then
  while IFS= read -r a || [ -n "$a" ]; do
    case "$a" in mode=*) PLY_MODE="${a#mode=}" ;; prev=*) PLY_PREV="${a#prev=}" ;; sha=*) PLY_SHA="${a#sha=}" ;; esac
  done < "$PLY_REC"
  case "$PLY_MODE" in create|replace|insert|append) ;; *) say "ignoré (marqueur Plymouth invalide)"; PLY_MODE="" ;; esac
  case "$PLY_PREV" in *[!0-9a-f]*) say "ignoré (thème précédent invalide)"; PLY_MODE="" ;; esac
  case "$PLY_SHA" in *[!0-9a-f]*|'') PLY_SHA="" ;; esac
fi
# initramfs : le NOM suffit à exiger une régénération (action sûre) ; contenu inattendu signalé, jamais ignoré
INITRD_REC_SEEN=0
if [ -e "$INITRD_REC" ] || [ -L "$INITRD_REC" ]; then
  INITRD_REC_SEEN=1
fi

# --- 0b') régénération précédente interrompue (coupure, arrêt brutal) : octets d'avant rétablis et vérifiés AVANT tout
if [ -e "$(dest /var/lib/opentv-install/initramfs.txn)" ] || [ -L "$(dest /var/lib/opentv-install/initramfs.txn)" ]; then
  if [ -z "$IR_LIB" ]; then
    boot_blocked "transaction initramfs interrompue mais opentv-initramfs.sh introuvable : rien modifié"
  elif [ "$DRY" = 1 ]; then
    say "[dry-run] transaction initramfs interrompue : octets d'avant restaurés puis vérifiés en premier"
  else
    boot_mounted "transaction initramfs interrompue : /boot/firmware/initramfs8 à rétablir"
    ir_recover || boot_blocked "transaction initramfs interrompue non rétablie (journal initramfs.txn conservé)"
    say "initramfs : transaction interrompue rejouée, /boot/firmware/initramfs8 rétabli octet pour octet"
  fi
fi

# --- 0b) vérifications : un seul refus ⇒ rien n'est modifié
first=""
cr=""
if [ ${#DROP[@]} -gt 0 ]; then
  boot_mounted "impossible de retirer ${DROP[*]} de cmdline.txt"
  [ -f "$CMDF" ] || boot_blocked "/boot/firmware/cmdline.txt introuvable : impossible de retirer ${DROP[*]}"
  IFS= read -r first < "$CMDF" || true
  case "$first" in *$'\r') cr=$'\r'; first="${first%$'\r'}" ;; esac
  case " $first " in *" root="*) ;; *) boot_blocked "cmdline.txt : première ligne sans root=… (fichier inattendu), non modifié" ;; esac
fi
if [ -n "$CFG_SFX" ]; then
  boot_mounted "impossible de retirer disable_splash=1 de config.txt"
  safe_text "$CFGF" 65536 || boot_blocked "/boot/firmware/config.txt absent ou inattendu : impossible de retirer disable_splash=1"
fi
if [ -n "$PLY_MODE" ] && { [ -e "$PLYC" ] || [ -L "$PLYC" ]; }; then
  safe_text "$PLYC" 65536 || boot_blocked "/etc/plymouth/plymouthd.conf inattendu (lien, non régulier, trop grand) : thème non restauré"
fi
if [ "$INITRD_REC_SEEN" = 1 ]; then
  safe_text "$INITRD_REC" 4096 ||
    boot_blocked "/var/lib/opentv-install/initramfs-plymouth.rebuilt inattendu (lien, non régulier, trop grand) : initramfs non régénéré"
  if [ "$(cat -- "$INITRD_REC")" != "$INITRD_REC_LINE" ]; then
    say "marqueur initramfs au contenu inattendu : régénération quand même (action sûre)"
  fi
fi

# --- 0c) cmdline.txt
if [ ${#DROP[@]} -gt 0 ]; then
  new="$first"
  for a in "${DROP[@]}"; do new="$(drop_arg "$new" "$a")"; done
  cmdline_new() {   # nouvelle première ligne ; CR et lignes suivantes conservés
    local nl=""
    if [ "$(wc -l < "$CMDF" | tr -d ' ')" -gt 0 ]; then nl=$'\n'; fi
    printf '%s%s%s' "$new" "$cr" "$nl" && tail -n +2 -- "$CMDF"
  }
  if [ "$new" = "$first" ]; then
    say "cmdline.txt : jetons OpenTV déjà absents, inchangé"
  elif [ "$DRY" = 1 ]; then
    say "[dry-run] retirer ${DROP[*]} de /boot/firmware/cmdline.txt"
  elif put_text "$CMDF" cmdline_new; then
    say "cmdline.txt : ${DROP[*]} retiré(s) (effectif au prochain redémarrage)"
  else
    rm -f -- "$CMDF.opentv-tmp" 2>/dev/null || true
    boot_blocked "écriture durable de cmdline.txt impossible ou non confirmée (support en lecture seule ?)"
  fi
fi

# --- 0d) config.txt : suffixe intact ⇒ retiré à l'octet près ; sinon dernière ligne « disable_splash=1 » (et son
#     « [all] » ajouté juste avant, s'il est noté), le reste intact
if [ -n "$CFG_SFX" ]; then
  n=$((${#CFG_SFX} / 2))
  size="$(wc -c < "$CFGF" | tr -d ' ')"
  CFG_ACTION="write"
  if [ "$size" -ge "$n" ] && [ "$(tailhex "$CFGF" "$n")" = "$CFG_SFX" ]; then
    keep=$((size - n))
    cfg_new() { if [ "$keep" -gt 0 ]; then head -c "$keep" -- "$CFGF"; fi; }
  else
    L=0
    i=0
    while IFS= read -r a || [ -n "$a" ]; do
      i=$((i + 1))
      [ "${a%$'\r'}" = disable_splash=1 ] && L=$i
    done < "$CFGF"
    if [ "$L" -gt 0 ]; then
      from=$((L - 1))
      # « [all] » ajouté par OpenTV : retiré seulement si aucune ligne non vide ne le suit — sinon conservé, pour que
      # les lignes ajoutées depuis n'héritent jamais de la section conditionnelle précédente ([cm4], [pi5]…)
      case "$CFG_SFX" in *5b616c6c5d*)
        if [ "$L" -gt 1 ] && [ "$(head -n $((L - 1)) -- "$CFGF" | tail -n 1 | tr -d '\r\n')" = "[all]" ]; then
          if [ "$(tail -n +$((L + 1)) -- "$CFGF" | tr -d ' \t\r\n' | wc -c | tr -d ' ')" = 0 ]; then from=$((L - 2))
          else say "config.txt : [all] ajouté par OpenTV conservé (des lignes le suivent)"; fi
        fi ;;
      esac
      cfg_new() { lines_upto "$from" "$CFGF"; tail -n +$((L + 1)) -- "$CFGF"; }
      say "config.txt : modifié depuis l'installation, seule la ligne disable_splash=1 ajoutée par OpenTV est retirée"
    else
      say "config.txt : disable_splash=1 d'OpenTV déjà absent, inchangé"
      CFG_ACTION=none
    fi
  fi
  if [ "$CFG_ACTION" = none ]; then :
  elif [ "$DRY" = 1 ]; then say "[dry-run] retirer disable_splash=1 (ajouté par OpenTV) de /boot/firmware/config.txt"
  elif put_text "$CFGF" cfg_new; then say "config.txt : disable_splash=1 retiré (arc-en-ciel du firmware rétabli au prochain redémarrage)"
  else
    rm -f -- "$CFGF.opentv-tmp" 2>/dev/null || true
    boot_blocked "écriture durable de config.txt impossible ou non confirmée (support en lecture seule ?)"
  fi
fi

# --- 0e) plymouthd.conf : fichier inchangé depuis l'installation ⇒ contenu d'origine exact ; modifié depuis ⇒ seule
#     la ligne Theme=opentv est rendue (thème précédent) ou retirée ; thème changé localement depuis ⇒ conservé
PLY_RESTORED=0
if [ -n "$PLY_MODE" ]; then
  ply_new=""
  if [ -f "$PLYC" ] && [ -n "$PLY_SHA" ] && [ "$(sha256 "$PLYC")" = "$PLY_SHA" ]; then
    if [ "$PLY_MODE" = create ]; then ply_new=remove
    elif safe_text "$PLY_ORIG" 65536; then ply_new=orig
    fi
  elif [ ! -e "$PLYC" ]; then
    if [ "$PLY_MODE" != create ] && safe_text "$PLY_ORIG" 65536; then ply_new=orig; else ply_new=none; fi
  fi
  if [ -z "$ply_new" ]; then
    ply_scan "$PLYC"
    if [ "$PLY_TVAL" = opentv ]; then
      L="$PLY_TLINE"
      eol=""
      [ "$(head -n "$L" -- "$PLYC" | tail -c 1 | hexof)" = 0a ] && eol=$'\n'
      ply_lines() { lines_upto $((L - 1)) "$PLYC"; if [ -n "$PLY_PREV" ]; then unhex "$PLY_PREV"; printf '%s' "$eol"; fi; tail -n +$((L + 1)) -- "$PLYC"; }
      ply_new=lines
      say "plymouthd.conf : modifié depuis l'installation, seule la ligne Theme=opentv est rendue"
    else
      say "plymouthd.conf : thème « ${PLY_TVAL:-défaut} » choisi localement depuis l'installation, conservé"
      ply_new=none
    fi
  fi
  orig_out() { cat -- "$PLY_ORIG"; }
  case "$ply_new" in
    none) ;;
    *)
      if [ "$DRY" = 1 ]; then say "[dry-run] Plymouth : thème précédent rétabli dans /etc/plymouth/plymouthd.conf"
      else
        case "$ply_new" in
          remove) rm -f -- "$PLYC" && durable_sync "$(dirname "$PLYC")" ;;
          orig) put_text "$PLYC" orig_out ;;
          lines) put_text "$PLYC" ply_lines ;;
        esac || { rm -f -- "$PLYC.opentv-tmp" 2>/dev/null || true; boot_blocked "écriture de /etc/plymouth/plymouthd.conf impossible"; }
        say "Plymouth : thème précédent rétabli (/etc/plymouth/plymouthd.conf)"
      fi ;;
  esac
  PLY_RESTORED=1
fi
for rec in "$VIDEO_REC" "$BOOT_REC" "$CFG_REC" "$PLY_REC" "$PLY_ORIG"; do   # traité : marqueurs effacés seulement maintenant
  if [ -f "$rec" ]; then act rm -f -- "$rec"; fi
done

# restore_backups <préfixe> : fichiers d'origine sauvegardés (BACKUP.list, dans l'ordre des installations) dont le
# chemin commence par <préfixe> (vide : tous), rétablis seulement là où rien n'existe plus
restore_backups() {
  local list bk p
  for list in "$REC_DIR"/backup-*/BACKUP.list; do
    [ -f "$list" ] || continue
    bk="$(dirname "$list")"
    while read -r p; do
      [ -n "$p" ] || continue
      case "$p" in "$1"*) ;; *) continue ;; esac
      if [ -f "$bk$p" ] && [ ! -e "$(dest "$p")" ]; then
        act mkdir -p "$(dirname "$(dest "$p")")"
        act cp -p -- "$bk$p" "$(dest "$p")"
        say "restauré : $p"
      fi
    done < "$list"
  done
}
THEME_PFX="/usr/share/plymouth/themes/opentv"
# theme_restore : thème tel qu'avant l'installation AVANT la régénération (le crochet copie le thème choisi : un
# Theme=opentv antérieur à OpenTV doit retrouver son propre contenu, ou son absence) — fichiers du manifeste retirés,
# originaux sauvegardés rétablis, dossier créé par OpenTV retiré s'il est vide. L'étape 4 reste idempotente.
theme_restore() {
  local p d
  [ -f "$MANIFEST" ] || return 0
  while read -r _ p; do
    case "$p" in "$THEME_PFX"/*) ;; *) continue ;; esac
    if [ -e "$(dest "$p")" ] || [ -L "$(dest "$p")" ]; then rm -f -- "$(dest "$p")" || return 1; fi
  done < "$MANIFEST"
  restore_backups "$THEME_PFX/"
  if [ -f "$DIRS_REC" ]; then
    while IFS= read -r d; do
      if [ "$d" = "$THEME_PFX" ] && [ -d "$(dest "$d")" ] && [ ! -L "$(dest "$d")" ]; then
        rmdir -- "$(dest "$d")" 2>/dev/null || say "conservé (non vide) : $d"
      fi
    done < "$DIRS_REC"
  fi
}
# initramfs_stale <liste> : entrées du thème opentv sans fichier correspondant sur le disque (charge OpenTV périmée)
initramfs_stale() {
  local e n
  while IFS= read -r e; do
    case "$e" in
      *plymouth/themes/opentv) [ -d "$(dest "$THEME_PFX")" ] || printf '%s\n' "$e" ;;
      *plymouth/themes/opentv/*)
        n="${e##*plymouth/themes/opentv/}"
        [ -e "$(dest "$THEME_PFX/$n")" ] || printf '%s\n' "$e" ;;
    esac
  done <<EOF
$1
EOF
}

# --- 0f) initramfs régénéré avec le thème rétabli dès qu'OpenTV l'a régénéré (marqueur) ou a rendu le thème
#     précédent. Transaction (opentv-initramfs.sh) : noyau v8 exact (jamais -k all) ; copie durable de chaque fichier
#     réécrivable (SHA-256) + journal AVANT ; régénération bornée par SIGTERM (jamais SIGKILL de la copie en place du
#     crochet) ; vérification lsinitramfs (aucune entrée opentv périmée) ET initramfs8 identique à initrd.img-<v>.
#     Échec : octets d'avant restaurés et relus, PUIS arrêt (code 1) AVANT l'arrêt des services et le retrait des
#     fichiers : /opt/opentv/uninstall.sh, sa bibliothèque et le marqueur initramfs-plymouth.rebuilt restent pour
#     relancer (même avec --purge).
ir_stop() {   # ir_stop <raison> [restaurer] : arrêt avant toute autre étape, retry possible
  local why="$1" kept="initramfs inchangé"
  if [ "${2:-}" = restore ]; then
    if [ "${IR_LIVE_WRITER:-0}" = 1 ]; then
      kept="régénération encore active : initramfs NON restauré maintenant (journal initramfs.txn conservé, rejoué au prochain essai)"
    elif ir_restore "$UBK" && ir_txn_end; then
      kept="initramfs rétabli octet pour octet (octets d'avant cette désinstallation, SHA-256 vérifiés, amorçable)"
    else
      kept="initramfs NON rétabli (journal initramfs.txn et sauvegarde $UBK conservés, rejoués au prochain essai)"
    fi
  elif [ "${2:-}" = txn ]; then
    ir_txn_end || true
  fi
  echo "opentv-uninstall: initramfs : $why" >&2
  echo "opentv-uninstall: $kept" >&2
  echo "opentv-uninstall: désinstallation INTERROMPUE avant l'arrêt des services et le retrait des fichiers :" \
    "/opt/opentv/uninstall.sh et le marqueur /var/lib/opentv-install/initramfs-plymouth.rebuilt sont conservés ;" \
    "corriger puis relancer : sudo /opt/opentv/uninstall.sh" >&2
  exit 1
}
IR_LIVE_WRITER=0
if [ "$PLY_RESTORED" = 1 ] || [ "$INITRD_REC_SEEN" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    say "[dry-run] thème Plymouth opentv rétabli tel qu'avant l'installation, puis initramfs : sauvegarde durable (SHA-256)," \
      "update-initramfs -u -k <noyau v8 exact> (SIGTERM borné, jamais SIGKILL), lsinitramfs et initramfs8 = initrd.img"
  else
    [ -n "$IR_LIB" ] || ir_stop "opentv-initramfs.sh introuvable : régénération impossible"
    boot_mounted "initramfs à régénérer"
    if [ "$LIVE" = 1 ] && ! command -v update-initramfs >/dev/null 2>&1; then ir_stop "update-initramfs introuvable"; fi
    # noyau exact de l'initramfs8 (rétabli plus haut si un journal restait ; noyau du journal préféré s'il est validé)
    ir_pick_kernel "${IR_TXN_KERNEL:-}" || ir_stop "aucun noyau v8 complet (/boot/initrd.img-<version>-v8 et modules) : régénération impossible"
    UBK="/var/lib/opentv-install/initramfs-uninstall-$TS-$$"
    ir_backup "$UBK" "$IR_KERNEL" || ir_stop "sauvegarde durable de l'initramfs impossible : rien régénéré"
    ir_txn_begin "$UBK" "$IR_KERNEL" uninstall || ir_stop "journal initramfs.txn impossible : rien régénéré" txn
    theme_restore || ir_stop "thème opentv non retiré : rien régénéré" txn
    say "initramfs : régénération du noyau $IR_KERNEL sans le thème OpenTV (peut prendre quelques minutes)"
    rc=0
    ir_rebuild "$IR_KERNEL" || rc=$?
    if [ "$rc" != 0 ]; then
      [ "$rc" = 125 ] && IR_LIVE_WRITER=1
      ir_stop "update-initramfs -u -k $IR_KERNEL a échoué ou dépassé le délai" restore
    fi
    if [ "$LIVE" = 1 ] || [ "${OPENTV_INSTALL_TEST_BOOTCMDS:-}" = 1 ]; then
      ULIST="$(dest "$UBK")/initramfs.list"
      ir_list "$ULIST" || ir_stop "relecture de $INITRD impossible (lsinitramfs en échec ou au-delà de $IR_LS_S s)" restore
      stale="$(initramfs_stale "$(cat -- "$ULIST")")"
      [ -z "$stale" ] || ir_stop "thème opentv périmé encore présent dans $INITRD" restore
      ir_same_as_source "$IR_KERNEL" ||
        ir_stop "$INITRD différent de /boot/initrd.img-$IR_KERNEL (copie du crochet incomplète)" restore
      if grep -qF -- "plymouth/themes/opentv/" "$ULIST"; then
        say "initramfs : régénéré ; thème opentv d'origine (antérieur à OpenTV) conservé"
      else
        say "initramfs : régénéré sans le thème opentv"
      fi
    fi
    # vérifié : marqueur effacé durablement, PUIS journal et sauvegarde de cette désinstallation
    if [ "$INITRD_REC_SEEN" = 1 ]; then
      { rm -f -- "$INITRD_REC" && durable_sync "$REC_DIR"; } || ir_stop "marqueur initramfs-plymouth.rebuilt non effacé" txn
    fi
    ir_txn_end || ir_stop "journal initramfs.txn non effacé"
    rm -rf -- "$(dest "$UBK")"
  fi
fi

# 1) plus rien ne peut (re)lancer le point d'accès : service, helper et garde arrêtés
host systemctl disable --now opentv-player.service opentv-player-keys.service opentv-kiosk.service opentv.service \
  opentv-net-helper.path opentv-net-helper.service opentv-ap-guard.timer opentv-ap-guard.service opentv-cec.timer \
  opentv-cec.service opentv-cec-announce.service opentv-splash.service opentv-catalogue-refresh.timer \
  opentv-catalogue-refresh.service opentv-plymouth-failsafe.timer opentv-plymouth-failsafe.service \
  opentv-plymouth-deactivate.service opentv-plymouth-quit.service
# « Regarder » : la promotion terrain ne survit jamais à une désinstallation (même sans --purge) — une réinstallation
# exige une nouvelle validation HDMI ; la source approuvée (source.env, configuration locale) reste jusqu'à --purge
PROMO="$(dest /etc/opentv/playback-promoted)"
if [ -e "$PROMO" ] || [ -L "$PROMO" ]; then act rm -f -- "$PROMO"; say "« Regarder » : promotion retirée"; fi
# 2) Wi-Fi temporaire OpenTV arrêté et supprimé AVANT tout retrait de fichier (normal comme --purge) ; argv fixes
NM="/etc/NetworkManager/system-connections"
AP_ID="opentv-setup-ap"
host nmcli --wait 10 connection down id "$AP_ID"
if [ -e "$(dest "$NM/$AP_ID.nmconnection")" ] || [ -L "$(dest "$NM/$AP_ID.nmconnection")" ]; then
  act rm -f -- "$(dest "$NM/$AP_ID.nmconnection")"
fi
host nmcli connection delete id "$AP_ID"
say "Wi-Fi temporaire OpenTV ($AP_ID) arrêté et supprimé"
# profil candidat d'un wifi-apply interrompu (autoconnect=false, jamais promu) : retiré, l'opentv-wifi actif reste
CAND_ID="opentv-wifi-candidate"
if [ -e "$(dest "$NM/$CAND_ID.nmconnection")" ] || [ -L "$(dest "$NM/$CAND_ID.nmconnection")" ]; then
  act rm -f -- "$(dest "$NM/$CAND_ID.nmconnection")"
fi
host nmcli connection delete id "$CAND_ID"
# 3) CEC : retour arrière borné AVANT le retrait des fichiers (le code est encore installé). N'efface que la
#    configuration Playback « OpenTV » et ne rétablit que la touche Select modifiée par OpenTV ; témoin /run retiré.
if [ -f "$(dest /opt/opentv/bin/opentv-pi)" ]; then
  host python3 -I /opt/opentv/bin/opentv-pi cec-bootstrap --release
fi
if [ -e "$(dest /run/opentv-cec)" ] || [ -L "$(dest /run/opentv-cec)" ]; then act rm -rf -- "$(dest /run/opentv-cec)"; fi
# 4) fichiers

if [ -f "$MANIFEST" ]; then
  while read -r _ p; do
    case "$p" in /opt/opentv/*|/etc/systemd/system/opentv*|/etc/sysusers.d/opentv.conf|/etc/tmpfiles.d/opentv.conf|/etc/pam.d/opentv-kiosk) ;;
      /etc/systemd/logind.conf.d/opentv-kiosk.conf|/etc/chromium/policies/managed/opentv-kiosk.json) ;;
      /etc/chromium-browser/policies/managed/opentv-kiosk.json) ;;
      /etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf) ;;
      /usr/share/plymouth/themes/opentv/*) ;;
      *) [ -z "$p" ] || say "ignoré (hors périmètre OpenTV) : $p"; continue ;; esac
    if [ -e "$(dest "$p")" ]; then act rm -f -- "$(dest "$p")"; fi
  done < "$MANIFEST"
  restore_backups ""   # restauration des fichiers d'origine (dans l'ordre des installations)
  act rm -f -- "$MANIFEST"
  act rm -rf -- "$(dest /opt/opentv/lib/opentv_pi/__pycache__)" "$(dest /opt/opentv/lib/foryou/__pycache__)"
  for d in /opt/opentv/lib/opentv_pi /opt/opentv/lib/foryou /opt/opentv/lib /opt/opentv/share/ui /opt/opentv/share/chromium-player-policy /opt/opentv/share \
           /opt/opentv/bin /opt/opentv; do
    if [ -d "$(dest "$d")" ]; then
      if [ "$DRY" = 1 ]; then say "[dry-run] rmdir $(dest "$d")"; else rmdir -- "$(dest "$d")" 2>/dev/null || say "conservé (non vide) : $d"; fi
    fi
  done
else
  say "manifeste absent : rien à retirer"
fi

# dossiers système créés par install.sh (liste fermée) : retirés seulement s'ils sont vides, du plus profond au plus haut
if [ -f "$DIRS_REC" ]; then
  DIRS=()
  while IFS= read -r d; do
    case "$d" in
      /etc/systemd/logind.conf.d|/etc/chromium|/etc/chromium/policies|/etc/chromium/policies/managed) DIRS+=("$d") ;;
      /etc/chromium-browser|/etc/chromium-browser/policies|/etc/chromium-browser/policies/managed) DIRS+=("$d") ;;
      /etc/systemd/system/getty@tty1.service.d) DIRS+=("$d") ;;
      /usr/share/plymouth|/usr/share/plymouth/themes|/usr/share/plymouth/themes/opentv) DIRS+=("$d") ;;
      *) [ -z "$d" ] || say "ignoré (hors périmètre OpenTV) : $d" ;;
    esac
  done < "$DIRS_REC"
  i=${#DIRS[@]}
  while [ "$i" -gt 0 ]; do
    i=$((i - 1))
    d="${DIRS[$i]}"
    if [ -d "$(dest "$d")" ] && [ ! -L "$(dest "$d")" ]; then
      if [ "$DRY" = 1 ]; then say "[dry-run] rmdir $(dest "$d")"; else rmdir -- "$(dest "$d")" 2>/dev/null || say "conservé (non vide) : $d"; fi
    fi
  done
  act rm -f -- "$DIRS_REC"
fi

host systemctl daemon-reload
# tty1 lisible tout de suite pour la console rendue (palette par défaut, curseur visible, police console de la
# distribution) ; best effort, chaque accès borné (une console bloquée ne fige jamais la désinstallation) ; le reste
# au redémarrage
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then
  if [ -c /dev/tty1 ] && command -v timeout >/dev/null 2>&1; then
    printf '\033]R\033[0m\033[?25h' | timeout 2 tee -- /dev/tty1 >/dev/null 2>&1 || true
    if command -v setupcon >/dev/null 2>&1; then
      timeout 10 setupcon --force --font-only </dev/null >/dev/null 2>&1 || true
    elif command -v setfont >/dev/null 2>&1; then
      timeout 3 setfont -C /dev/tty1 </dev/null >/dev/null 2>&1 || true
    fi
  fi
else
  say "[plan] tty1 : palette par défaut, curseur visible, police console (best effort, borné)"
fi
host systemctl start getty@tty1.service

if [ "$PURGE" = 1 ]; then
  for d in /var/lib/opentv /var/lib/opentv-kiosk /etc/opentv /var/lib/opentv-install; do
    # (un échec initramfs s'arrête à l'étape 0f, avant ce point : le marqueur et la sauvegarde y restent)
    if [ -e "$(dest "$d")" ]; then act rm -rf -- "$(dest "$d")"; fi
  done
  if [ "$FORGET_WIFI" = 1 ] && [ -e "$(dest "$NM/opentv-wifi.nmconnection")" ]; then
    act rm -f -- "$(dest "$NM/opentv-wifi.nmconnection")"
  fi
  host nmcli connection reload
  say "comptes système conservés (sans effet) : sudo userdel opentv; sudo userdel opentv-kiosk; sudo userdel opentv-keys; sudo groupdel opentv-agent"
fi
say "désinstallation terminée"
