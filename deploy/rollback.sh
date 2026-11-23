#!/bin/sh
set -eu
umask 077
STAMP=$(cat)
case "$STAMP" in *[!0-9TZ]*|'') exit 1 ;; esac
[ "${#STAMP}" -eq 16 ]
systemctl stop mesh.service
cp "/var/lib/mesh/backups/mesh.pre-upgrade.$STAMP" /opt/mesh/mesh
database=$(cat "/var/lib/mesh/backups/mesh.database-path.$STAMP")
# Both service names are stopped before restoring the SQLite snapshot.
systemctl stop daochi.service || true
rm -f "$database-wal" "$database-shm"
cp "/var/lib/mesh/backups/mesh.db.$STAMP" "$database"
cp "/var/lib/mesh/backups/mesh.env.$STAMP" /etc/mesh/mesh.env
if [ -f "/var/lib/mesh/backups/mesh.manifest.$STAMP" ]; then
  cp "/var/lib/mesh/backups/mesh.manifest.$STAMP" /opt/mesh/mesh.manifest.json
else
  rm -f /opt/mesh/mesh.manifest.json
fi
for name in mesh daochi; do
  if [ -f "/var/lib/mesh/backups/mesh.$name.$STAMP" ]; then
    install -m 600 "/var/lib/mesh/backups/mesh.$name.$STAMP" "/etc/systemd/system/mesh.service.d/20-$name-mesh.conf"
  elif [ -f "/var/lib/mesh/backups/mesh.$name-absent.$STAMP" ]; then
    rm -f "/etc/systemd/system/mesh.service.d/20-$name-mesh.conf"
  fi
done
if [ -f "/var/lib/mesh/backups/mesh-native.pre-upgrade.$STAMP" ]; then
  cp "/var/lib/mesh/backups/mesh-native.pre-upgrade.$STAMP" /opt/mesh/mesh-native
elif [ -f "/var/lib/mesh/backups/mesh-native-absent.$STAMP" ]; then
  rm -f /opt/mesh/mesh-native /opt/mesh/mesh-native.manifest.json
fi
if [ -f "/var/lib/mesh/backups/mesh.runtime.$STAMP" ]; then
  install -m 644 "/var/lib/mesh/backups/mesh.runtime.$STAMP" /etc/systemd/system/mesh.service.d/30-runtime.conf
elif [ -f "/var/lib/mesh/backups/mesh.runtime-absent.$STAMP" ]; then
  rm -f /etc/systemd/system/mesh.service.d/30-runtime.conf
fi
sh /opt/mesh-src/deploy/harden-service.sh restore "$STAMP"
if [ -f "/var/lib/mesh/backups/mesh.sandbox.$STAMP" ]; then
  install -m 644 "/var/lib/mesh/backups/mesh.sandbox.$STAMP" /etc/systemd/system/mesh.service.d/10-sandbox.conf
elif [ -f "/var/lib/mesh/backups/mesh.sandbox-absent.$STAMP" ]; then
  rm -f /etc/systemd/system/mesh.service.d/10-sandbox.conf
fi
systemctl daemon-reload
if [ -f "/var/lib/mesh/backups/mesh.legacy-prune-enabled.$STAMP" ]; then
  systemctl disable --now mesh-backup-prune.timer || true
  systemctl enable daochi-backup-prune.timer || true
  if [ -f "/var/lib/mesh/backups/mesh.legacy-prune-active.$STAMP" ]; then
    systemctl start daochi-backup-prune.timer || true
  fi
fi
if [ -f "/var/lib/mesh/backups/legacy-migration.$STAMP.json" ]; then
  python3 /opt/mesh-src/deploy/migrate-install.py restore "$STAMP"
else
  systemctl start mesh.service
fi
