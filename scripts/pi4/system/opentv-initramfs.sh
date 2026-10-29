# shellcheck shell=bash disable=SC2034
# OpenTV Pi : transaction initramfs partagée par install.sh et uninstall.sh (root ; sourcé, aucune action au chargement).
#
# Raspberry Pi OS Bookworm : « update-initramfs -u -k <v> » écrit /boot/initrd.img-<v> (renommage atomique), puis le
# crochet /etc/initramfs/post-update.d/z50-raspi-firmware le COPIE EN PLACE vers /boot/firmware/initramfs8 (troncature
# puis écriture, partition FAT) : une coupure ou un arrêt pendant cette copie laisse un initramfs8 tronqué. D'où :
#   1. noyau exact : le noyau v8 dont /boot/initrd.img-<v> est identique octet pour octet à initramfs8 (sinon le plus
#      récent noyau v8 complet, initrd + modules), choisi APRÈS le rejeu d'un journal resté (initramfs8 rétabli) ;
#      « update-initramfs -u -k <v> », jamais « -k all » ;
#   2. AVANT la régénération : copie durable (temp → fsync → renommage → fsync du dossier) de chaque fichier que cette
#      régénération peut réécrire (initramfs8, initrd.img-<v>, /var/lib/initramfs-tools/<v>), SHA-256 + taille + mode
#      notés, copies relues ; puis journal de transaction durable /var/lib/opentv-install/initramfs.txn ;
#   3. régénération bornée par SIGTERM seulement (jamais SIGKILL : la copie du crochet n'est jamais tuée brutalement),
#      puis attente bornée de la fin de TOUT le groupe de processus avant de toucher un fichier ;
#   4. vérification : lsinitramfs borné et entrées exigées, PUIS initramfs8 identique à initrd.img-<v> ;
#   5. tout échec : octets sauvegardés restaurés (temp du même dossier → fsync → renommage → fsync du dossier),
#      empreintes relues AVANT tout changement de marqueur. Un journal resté (coupure, arrêt brutal) est rejoué au
#      prochain install.sh / uninstall.sh avant toute autre action.
# Fournis par l'appelant : dest, say, sha256, durable_sync, bootcmd, LIVE. Aucune substitution $(…) d'une commande
# pouvant échouer (sous « set -E », le piège ERR d'install.sh s'exécuterait aussi dans le sous-shell).

IR_TXN="/var/lib/opentv-install/initramfs.txn"
IR_FW="/boot/firmware/initramfs8"
IR_KERNEL=""
IR_UPDATE_S=900    # update-initramfs (initramfs Pi 4 : < 2 min d'habitude, carte SD lente comprise)
IR_LS_S=300        # lsinitramfs : décompression complète d'une image zstd de 20-40 Mo, carte SD lente
IR_GRACE_S=120     # après SIGTERM : attente de la fin du groupe (copie du crochet) avant toute restauration
# ir_init : délais réduits pour les racines de test seulement (jamais sur la vraie racine)
ir_init() {
  if [ "$LIVE" = 0 ]; then
    case "${OPENTV_TEST_IR_UPDATE_S:-}" in ''|*[!0-9]*) ;; *) IR_UPDATE_S="$OPENTV_TEST_IR_UPDATE_S" ;; esac
    case "${OPENTV_TEST_IR_LS_S:-}" in ''|*[!0-9]*) ;; *) IR_LS_S="$OPENTV_TEST_IR_LS_S" ;; esac
    case "${OPENTV_TEST_IR_GRACE_S:-}" in ''|*[!0-9]*) ;; *) IR_GRACE_S="$OPENTV_TEST_IR_GRACE_S" ;; esac
  fi
  return 0
}

ir_valid_kernel() {   # version v8 (Pi 4, 64 bits) sans caractère inattendu
  case "$1" in ''|.*|*[!A-Za-z0-9._+-]*) return 1 ;; *-v8) return 0 ;; esac
  return 1
}
ir_has_modules() { [ -d "$(dest "/lib/modules/$1")" ] || [ -d "$(dest "/usr/lib/modules/$1")" ]; }
ir_regular() { [ -f "$1" ] && [ ! -L "$1" ]; }

# ir_pick_kernel [noyau préféré] : IR_KERNEL = noyau v8 validé (1 si aucun). À appeler hors région piégée (sort,
# sha256) et APRÈS ir_recover (un initramfs8 tronqué ne correspond à aucun noyau). Le noyau préféré (celui du journal
# rejoué) n'est retenu que s'il est lui-même validé ET identique octet pour octet à initramfs8 ; sinon ignoré.
ir_kernel_ok() { ir_valid_kernel "$1" && ir_has_modules "$1" && ir_regular "$(dest "/boot/initrd.img-$1")"; }
ir_pick_kernel() {
  local f k fw fwsha list="" pref="${1:-}"
  IR_KERNEL=""
  fw="$(dest "$IR_FW")"
  ir_regular "$fw" || return 1
  fwsha="$(sha256 "$fw")"
  if [ -n "$pref" ] && ir_kernel_ok "$pref" && [ "$(sha256 "$(dest "/boot/initrd.img-$pref")")" = "$fwsha" ]; then
    IR_KERNEL="$pref"
    return 0
  fi
  for f in "$(dest /boot)"/initrd.img-*; do
    ir_regular "$f" || continue
    k="${f##*/initrd.img-}"
    if ! ir_valid_kernel "$k" || ! ir_has_modules "$k"; then continue; fi
    list="$list$k"$'\n'
    if [ -z "$IR_KERNEL" ] && [ "$(sha256 "$f")" = "$fwsha" ]; then IR_KERNEL="$k"; fi
  done
  [ -n "$IR_KERNEL" ] && return 0
  [ -n "$list" ] || return 1
  # aucune correspondance exacte : noyau v8 le plus récent (celui que le firmware chargera : kernel8.img)
  IR_KERNEL="$(printf '%s' "$list" | { sort -V 2>/dev/null || sort; } | tail -n 1)"
  [ -n "$IR_KERNEL" ]
}

ir_put() {   # ir_put <chemin réel> <contenu> : temp → fsync → renommage → fsync (fichier + dossier)
  printf '%s\n' "$2" > "$1.opentv-tmp" || return 1
  durable_sync "$1.opentv-tmp" || return 1
  mv -f -- "$1.opentv-tmp" "$1" || return 1
  durable_sync "$1" "${1%/*}"
}

# ir_backup <dossier logique> <noyau> : copie durable de chaque fichier réécrivable + meta « i sha taille mode chemin »
# (« i absent - - chemin » si absent : supprimé à la restauration). 1 si une copie ne relit pas la même empreinte.
ir_backup() {
  local d="$1" k="$2" i=0 p t h n m
  d="$(dest "$d")"
  mkdir -p -- "$d" || return 1
  : > "$d/meta.opentv-tmp" || return 1
  for p in "$IR_FW" "/boot/initrd.img-$k" "/var/lib/initramfs-tools/$k"; do
    t="$(dest "$p")"
    if [ -L "$t" ] || { [ -e "$t" ] && [ ! -f "$t" ]; }; then
      echo "opentv-initramfs: $p inattendu (lien ou non régulier) : rien sauvegardé" >&2
      return 1
    fi
    if [ ! -e "$t" ]; then
      printf '%s absent - - %s\n' "$i" "$p" >> "$d/meta.opentv-tmp" || return 1
    else
      cp -- "$t" "$d/$i.opentv-tmp" || return 1
      h="$(sha256 "$t")"
      [ -f "$d/$i.opentv-tmp" ] && [ "$(sha256 "$d/$i.opentv-tmp")" = "$h" ] || {
        echo "opentv-initramfs: copie de $p non conforme (SHA-256)" >&2; return 1; }
      durable_sync "$d/$i.opentv-tmp" || return 1
      mv -f -- "$d/$i.opentv-tmp" "$d/$i" || return 1
      n="$(wc -c < "$t" | tr -d ' ')"
      m="$(stat -c %a "$t" 2>/dev/null || stat -f %Lp "$t")"
      printf '%s %s %s %s %s\n' "$i" "$h" "$n" "$m" "$p" >> "$d/meta.opentv-tmp" || return 1
    fi
    i=$((i + 1))
  done
  durable_sync "$d/meta.opentv-tmp" || return 1
  mv -f -- "$d/meta.opentv-tmp" "$d/meta" || return 1
  durable_sync "$d/meta" "$d" "${d%/*}"
}

# ir_restore <dossier logique> : chaque fichier noté rendu à ses octets d'origine, empreinte relue (1 sinon)
ir_restore() {
  local d i h n m p t bad=0
  d="$(dest "$1")"
  ir_regular "$d/meta" || { echo "opentv-initramfs: sauvegarde $1/meta introuvable" >&2; return 1; }
  while read -r i h n m p; do
    case "$i" in ''|*[!0-9]*) bad=1; continue ;; esac
    case "$p" in "$IR_FW"|/boot/initrd.img-*|/var/lib/initramfs-tools/*) ;; *) bad=1; continue ;; esac
    case "$p" in *..*|*[!A-Za-z0-9._+/-]*) bad=1; continue ;; esac
    t="$(dest "$p")"
    if [ "$h" = absent ]; then
      if [ -e "$t" ] || [ -L "$t" ]; then
        if ! { rm -f -- "$t" && durable_sync "${t%/*}"; }; then bad=1; fi
      fi
      continue
    fi
    case "$m" in ''|*[!0-7]*) m=644 ;; esac
    if ! { ir_regular "$d/$i" && [ "$(sha256 "$d/$i")" = "$h" ]; }; then
      echo "opentv-initramfs: sauvegarde de $p altérée (SHA-256) : non restaurée" >&2
      bad=1
      continue
    fi
    if ir_regular "$t" && [ "$(sha256 "$t")" = "$h" ]; then continue; fi   # déjà intact
    rm -f -- "$t.opentv-tmp" 2>/dev/null
    if cp -- "$d/$i" "$t.opentv-tmp" && { chmod "$m" "$t.opentv-tmp" 2>/dev/null || true; } &&
       [ "$(sha256 "$t.opentv-tmp")" = "$h" ] && durable_sync "$t.opentv-tmp" && mv -f -- "$t.opentv-tmp" "$t" &&
       durable_sync "$t" "${t%/*}" && ir_regular "$t" && [ "$(sha256 "$t")" = "$h" ]; then
      echo "opentv-initramfs: $p restauré octet pour octet (SHA-256 $h)" >&2
    else
      rm -f -- "$t.opentv-tmp" 2>/dev/null
      echo "opentv-initramfs: restauration de $p impossible (sauvegarde : $1/$i)" >&2
      bad=1
    fi
  done < "$d/meta"
  [ "$bad" = 0 ]
}

ir_txn_begin() {   # ir_txn_begin <dossier logique> <noyau> <install|uninstall> : journal durable AVANT régénération
  mkdir -p -- "$(dest "${IR_TXN%/*}")" || return 1
  ir_put "$(dest "$IR_TXN")" "backup=$1
kernel=$2
op=$3"
}
ir_txn_end() { rm -f -- "$(dest "$IR_TXN")" && durable_sync "$(dest "${IR_TXN%/*}")"; }

# ir_txn_read <journal réel> : IR_TXN_BK = sauvegarde notée ; IR_TXN_KERNEL = noyau noté, seulement s'il est validé
# (version v8 sûre, modules et initrd.img présents) — jamais utilisé tel quel : ir_pick_kernel le revérifie.
IR_TXN_BK=""
IR_TXN_KERNEL=""
ir_txn_read() {
  local a k=""
  IR_TXN_BK=""; IR_TXN_KERNEL=""
  while IFS= read -r a || [ -n "$a" ]; do
    case "$a" in backup=*) IR_TXN_BK="${a#backup=}" ;; kernel=*) k="${a#kernel=}" ;; esac
  done < "$1"
  if [ -n "$k" ] && ir_kernel_ok "$k"; then IR_TXN_KERNEL="$k"
  elif [ -n "$k" ]; then echo "opentv-initramfs: noyau du journal ignoré (invalide ou incomplet)" >&2; fi
  return 0
}

# ir_recover : journal présent (régénération interrompue : coupure, arrêt brutal) ⇒ octets d'avant restaurés et
# vérifiés, PUIS journal effacé ; IR_RECOVERED=1. 1 si le journal est inattendu ou la restauration impossible.
# L'appelant choisit ENSUITE le noyau (ir_pick_kernel "$IR_TXN_KERNEL") sur l'initramfs8 rétabli.
IR_RECOVERED=0
ir_recover() {
  local t bk
  t="$(dest "$IR_TXN")"
  [ -e "$t" ] || [ -L "$t" ] || return 0
  if ! ir_regular "$t" || [ "$(wc -c < "$t" | tr -d ' ')" -gt 4096 ]; then
    echo "opentv-initramfs: journal $IR_TXN inattendu (lien, non régulier ou trop grand)" >&2
    return 1
  fi
  ir_txn_read "$t"
  bk="$IR_TXN_BK"
  case "$bk" in /var/lib/opentv-install/*) ;; *) echo "opentv-initramfs: journal $IR_TXN sans sauvegarde valide" >&2; return 1 ;; esac
  case "$bk" in *..*|*[!A-Za-z0-9._+/-]*) echo "opentv-initramfs: chemin de sauvegarde invalide : $bk" >&2; return 1 ;; esac
  echo "opentv-initramfs: régénération précédente interrompue : initramfs rétabli depuis $bk" >&2
  ir_restore "$bk" || return 1
  ir_txn_end || return 1
  IR_RECOVERED=1
}

# ir_rebuild <noyau> : update-initramfs -u -k <noyau>, borné par SIGTERM au groupe (jamais SIGKILL), puis attente de
# la fin de tout le groupe (copie du crochet) : 125 si un processus survit (aucune restauration ne doit le concurrencer)
ir_run_group() {
  local s="$1" pg rc=0 n=0
  shift
  if ! command -v timeout >/dev/null 2>&1; then "$@"; return; fi
  timeout -s TERM "$s" "$@" &   # GNU timeout : son propre groupe de processus (setpgid) ; SIGTERM à tout le groupe
  pg=$!
  wait "$pg" || rc=$?
  while kill -0 -- "-$pg" 2>/dev/null; do
    if [ "$n" -ge "$IR_GRACE_S" ]; then
      echo "opentv-initramfs: processus de régénération encore actifs après $IR_GRACE_S s (groupe $pg)" >&2
      return 125
    fi
    sleep 1
    n=$((n + 1))
  done
  [ "$rc" = 124 ] && echo "opentv-initramfs: update-initramfs : délai de $s s dépassé (SIGTERM)" >&2
  return "$rc"
}
ir_rebuild() { bootcmd ir_run_group "$IR_UPDATE_S" update-initramfs -u -k "$1"; }

# ir_list <fichier> : liste de initramfs8 (lsinitramfs borné, lecture seule)
ir_list() {
  local s="$IR_LS_S"
  if command -v timeout >/dev/null 2>&1; then bootcmd timeout -k 10 "$s" lsinitramfs "$(dest "$IR_FW")" > "$1"
  else bootcmd lsinitramfs "$(dest "$IR_FW")" > "$1"; fi
}
ir_has_entry() { grep -qxF -e "$1" -e "./$1" -- "$2"; }   # entrée exacte de la liste
ir_same_as_source() {   # initramfs8 identique à /boot/initrd.img-<noyau> (copie du crochet complète)
  ir_regular "$(dest "$IR_FW")" && ir_regular "$(dest "/boot/initrd.img-$1")" &&
    cmp -s -- "$(dest "$IR_FW")" "$(dest "/boot/initrd.img-$1")"
}
