#!/bin/bash
# Usage : ./deploy.sh <IP_TV> <OpenTV.wgt>   — envoie, installe, relance et vérifie OpenTV
set -e; T=${1:-192.168.2.14}; W=${2:-$HOME/mnt/Nova-TV-Free/OpenTV.wgt}; D=$(dirname "$0")
R=/home/owner/share/tmp/sdk_tools/OpenTV.wgt
node "$D/install.mjs" "$T" "$W" "$R" OpnTV00001 | grep -a PUSH
timeout 170 node "$D/sdb.mjs" "$T" "0 vd_appinstall OpnTV00001 $R" | grep -aE "completed|fail|rror|cmd_ret"
timeout 40 node "$D/sdb.mjs" "$T" "0 debug OpnTV00001.OpenTV" | grep -a launched   # relance à neuf (sinon l'instance préchargée reste)
for n in $(seq 1 25); do s=$(curl -s -m2 "http://$T:8096/status" || true); echo "$s" | grep -q '"client":{' && break; sleep 2; done
echo "ETAT: $s"
