#!/bin/sh
# Install the verified build with data backups and restore on a failed migration.
set -eu
umask 077
STAMP=${1:?backup timestamp required}
source_commit=${2:?source revision required}
expected_commit=${3:?native revision required}
case "$STAMP" in *[!0-9TZ]*|'') exit 1 ;; esac
[ "${#STAMP}" -eq 16 ]
for revision in "$source_commit" "$expected_commit"; do
  case "$revision" in *[!0-9a-f]*|'') exit 1 ;; esac
  [ "${#revision}" -eq 40 ]
done
mesh_token=$(cat)
# systemd shows Environment= values to local users, so the node sync token
# lives only in the 0600 /etc/mesh/mesh.env.
if [ -n "$mesh_token" ]; then
  echo "Set MESH_NODE_SYNC_TOKEN in /etc/mesh/mesh.env instead of passing it to the deploy" >&2
  exit 1
fi
rollback_ready=0
service_stopped=0
finish() {
  result=$?
  trap - 0
  if [ "$result" -ne 0 ] && [ "$rollback_ready" -eq 1 ]; then
    printf '%s' "$STAMP" | sh /opt/mesh-src/deploy/rollback.sh
  elif [ "$result" -ne 0 ] && [ "$service_stopped" -eq 1 ]; then
    systemctl start mesh.service
  fi
  if [ "$result" -ne 0 ]; then
    python3 /opt/mesh-src/deploy/migrate-install.py restore "$STAMP"
  fi
  exit "$result"
}
trap finish 0
python3 /opt/mesh-src/scripts/verify_deployment_inputs.py verify-artifact-manifest \
  --output /opt/mesh-src/mesh \
  --manifest /opt/mesh-src/build/mesh.manifest.json \
  --expected-source-commit "$source_commit" \
  --expected-liboqs-commit "$expected_commit"
# deploy/runtime selects the server mesh.service runs: go or native.
runtime=go
if [ -f /opt/mesh-src/deploy/runtime ]; then
  runtime=$(cat /opt/mesh-src/deploy/runtime)
fi
case "$runtime" in go|native) ;; *) exit 1 ;; esac
if [ -f /opt/mesh-src/mesh-native ]; then
  python3 /opt/mesh-src/scripts/verify_deployment_inputs.py verify-artifact-manifest \
    --output /opt/mesh-src/mesh-native \
    --manifest /opt/mesh-src/build/mesh-native.manifest.json \
    --expected-source-commit "$source_commit" \
    --expected-liboqs-commit "$expected_commit"
elif [ "$runtime" = native ]; then
  exit 1
fi
python3 /opt/mesh-src/deploy/migrate-install.py prepare "$STAMP"
mkdir -p /var/lib/mesh/backups
database=$(python3 /opt/mesh-src/deploy/database-path.py /etc/mesh/mesh.env)
test -f "$database"
printf '%s\n' "$database" > "/var/lib/mesh/backups/mesh.database-path.$STAMP"
cp /opt/mesh/mesh "/var/lib/mesh/backups/mesh.pre-upgrade.$STAMP"
if [ -f /opt/mesh/mesh.manifest.json ]; then
  cp /opt/mesh/mesh.manifest.json "/var/lib/mesh/backups/mesh.manifest.$STAMP"
fi
systemctl stop mesh.service
service_stopped=1
sqlite3 "$database" ".backup '/var/lib/mesh/backups/mesh.db.$STAMP'"
test -s "/var/lib/mesh/backups/mesh.db.$STAMP"
install -m 600 /etc/mesh/mesh.env "/var/lib/mesh/backups/mesh.env.$STAMP"
# Earlier deploys set the node sync token in these drop-ins.
for name in mesh daochi; do
  if [ -f "/etc/systemd/system/mesh.service.d/20-$name-mesh.conf" ]; then
    cp "/etc/systemd/system/mesh.service.d/20-$name-mesh.conf" "/var/lib/mesh/backups/mesh.$name.$STAMP"
  else
    touch "/var/lib/mesh/backups/mesh.$name-absent.$STAMP"
  fi
done
if [ -f /opt/mesh/mesh-native ]; then
  cp /opt/mesh/mesh-native "/var/lib/mesh/backups/mesh-native.pre-upgrade.$STAMP"
else
  touch "/var/lib/mesh/backups/mesh-native-absent.$STAMP"
fi
if [ -f /etc/systemd/system/mesh.service.d/30-runtime.conf ]; then
  cp /etc/systemd/system/mesh.service.d/30-runtime.conf "/var/lib/mesh/backups/mesh.runtime.$STAMP"
else
  touch "/var/lib/mesh/backups/mesh.runtime-absent.$STAMP"
fi
rollback_ready=1
# Replace the existing backup maintenance unit only when it was already enabled.
if systemctl is-enabled --quiet daochi-backup-prune.timer || systemctl is-enabled --quiet mesh-backup-prune.timer; then
  if systemctl is-enabled --quiet daochi-backup-prune.timer; then
    touch "/var/lib/mesh/backups/mesh.legacy-prune-enabled.$STAMP"
    if systemctl is-active --quiet daochi-backup-prune.timer; then
      touch "/var/lib/mesh/backups/mesh.legacy-prune-active.$STAMP"
    fi
    systemctl disable --now daochi-backup-prune.timer
  fi
  install -m 644 /opt/mesh-src/deploy/mesh-backup-prune.service /opt/mesh-src/deploy/mesh-backup-prune.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable mesh-backup-prune.timer
  if [ -f "/var/lib/mesh/backups/mesh.legacy-prune-active.$STAMP" ]; then
    systemctl start mesh-backup-prune.timer
  fi
fi
install -m 755 /opt/mesh-src/mesh /opt/mesh/mesh.new
install -m 644 /opt/mesh-src/build/mesh.manifest.json /opt/mesh/mesh.manifest.new
mv /opt/mesh/mesh.new /opt/mesh/mesh
mv /opt/mesh/mesh.manifest.new /opt/mesh/mesh.manifest.json
# The Go binary stays installed: its CLI commands and apply-dew-ratio.sh use it.
if [ -f /opt/mesh-src/mesh-native ]; then
  install -m 755 /opt/mesh-src/mesh-native /opt/mesh/mesh-native.new
  install -m 644 /opt/mesh-src/build/mesh-native.manifest.json /opt/mesh/mesh-native.manifest.new
  mv /opt/mesh/mesh-native.new /opt/mesh/mesh-native
  mv /opt/mesh/mesh-native.manifest.new /opt/mesh/mesh-native.manifest.json
fi
install -d -m 755 /etc/systemd/system/mesh.service.d
# The native server loads Avahi Core at runtime for LAN discovery. Without it,
# native drops advertisement that the Go binary provided.
if [ "$runtime" = native ] && ! ldconfig -p | grep -q 'libavahi-core\.so\.7'; then
  apt-get install -y libavahi-core7 >/dev/null
fi
if [ "$runtime" = native ]; then
  printf '[Service]\nExecStart=\nExecStart=/opt/mesh/mesh-native\n' > /etc/systemd/system/mesh.service.d/30-runtime.new
  chmod 644 /etc/systemd/system/mesh.service.d/30-runtime.new
  mv /etc/systemd/system/mesh.service.d/30-runtime.new /etc/systemd/system/mesh.service.d/30-runtime.conf
else
  rm -f /etc/systemd/system/mesh.service.d/30-runtime.conf
fi
rm -f /etc/systemd/system/mesh.service.d/20-mesh-mesh.conf /etc/systemd/system/mesh.service.d/20-daochi-mesh.conf
systemctl daemon-reload
python3 /opt/mesh-src/scripts/migrate-legacy-env.py /etc/mesh/mesh.env --apply --canonical-only
sh /opt/mesh-src/deploy/set-environment.sh /opt/mesh-src/deploy/node.env /etc/mesh/mesh.env
if [ -s /etc/mesh/google-play-service-account.json ]; then
  echo MESH_GOOGLE_SERVICE_ACCOUNT_JSON_FILE=/etc/mesh/google-play-service-account.json |
    sh /opt/mesh-src/deploy/set-environment.sh - /etc/mesh/mesh.env
fi
# A raised Dew ratio or a new Dew product in deploy/dew-ratio.env
# rewrites the environment; if that fails, the previous node comes back.
sh /opt/mesh-src/deploy/apply-dew-ratio.sh /opt/mesh-src/deploy/dew-ratio.env /etc/mesh/mesh.env /opt/mesh/mesh "$database"
systemctl start mesh.service
sleep 3
systemctl is-active --quiet mesh.service
rollback_ready=0
