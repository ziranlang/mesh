#!/bin/sh
# Run the Mesh API server as a dedicated non-root account inside a sandbox.
#
#   record STAMP    save the current owner and mode of the data and config trees
#   apply           create the account, hand over the data dir, install the drop-in
#   restore STAMP   put back the saved owner/mode and remove the drop-in
#
# The root-owned configuration stays root-owned and readable by the mesh
# group. Only the data directory and the deploy backup directory are writable.
# The origin identifies the account by name and fixed id so a reinstall keeps
# the same uid and gid.
set -eu

ACCOUNT=mesh
GROUP=mesh
USER_ID=987
GROUP_ID=987
DATA_DIR=/var/lib/daochi
BACKUP_DIR=/var/lib/mesh/backups
CONFIG_DIR=/etc/mesh
DROPIN_DIR=/etc/systemd/system/mesh.service.d
DROPIN=$DROPIN_DIR/10-sandbox.conf
MANIFEST_DIR=/var/lib/mesh/backups

usage() {
    echo "usage: harden-service.sh record|apply|restore [STAMP]" >&2
    exit 2
}

manifest() {
    printf '%s/mesh.ownership.%s\n' "$MANIFEST_DIR" "$1"
}

record() {
    stamp=$1
    install -d -m 0700 "$MANIFEST_DIR"
    {
        printf '%s\n' "$DATA_DIR" "$BACKUP_DIR" "$CONFIG_DIR" "$DROPIN_DIR" "$DROPIN" |
            while IFS= read -r path; do
                [ -e "$path" ] || continue
                find "$path" -printf '%p\t%U\t%G\t%m\n'
            done
    } > "$(manifest "$stamp")"
}

# `apply` creates the service account once a host provides the tools. Tests
# pass --skip-account because a disposable tree has no passwd database.
apply() {
    if [ "${1:-}" != "--skip-account" ]; then
        if ! getent group "$GROUP" >/dev/null; then
            groupadd --system --gid "$GROUP_ID" "$GROUP"
        fi
        if ! getent passwd "$ACCOUNT" >/dev/null; then
            useradd --system --uid "$USER_ID" --gid "$GROUP" --home-dir /nonexistent \
                --shell /usr/sbin/nologin --comment "Mesh API Server" "$ACCOUNT"
        fi
    fi
    install -d -m 0700 -o "$ACCOUNT" -g "$GROUP" "$DATA_DIR"
    chown -R "$ACCOUNT:$GROUP" "$DATA_DIR"
    install -d -m 0750 -o "$ACCOUNT" -g "$GROUP" "$BACKUP_DIR"
    # Root keeps configuration ownership; the service only needs to read it.
    if [ -d "$CONFIG_DIR" ]; then
        chgrp -R "$GROUP" "$CONFIG_DIR"
        chmod 0750 "$CONFIG_DIR"
        find "$CONFIG_DIR" -type d -exec chmod g+rx {} +
        find "$CONFIG_DIR" -type f -exec chmod g+r {} +
    fi
    install -d -m 0755 "$DROPIN_DIR"
    cat > "$DROPIN_DIR/10-sandbox.new" <<'UNIT'
[Service]
User=mesh
Group=mesh
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=/var/lib/daochi /var/lib/mesh/backups
UNIT
    chmod 0644 "$DROPIN_DIR/10-sandbox.new"
    mv "$DROPIN_DIR/10-sandbox.new" "$DROPIN"
}

restore() {
    stamp=$1
    saved=$(manifest "$stamp")
    if [ -f "$saved" ]; then
        while IFS='	' read -r path uid gid mode; do
            [ -e "$path" ] || continue
            chown "$uid:$gid" "$path"
            chmod "$mode" "$path"
        done < "$saved"
    fi
    rm -f "$DROPIN"
}

case "${1:-}" in
    record)
        [ "$#" -eq 2 ] || usage
        record "$2"
        ;;
    apply)
        apply "${2:-}"
        ;;
    restore)
        [ "$#" -eq 2 ] || usage
        restore "$2"
        ;;
    *) usage ;;
esac
