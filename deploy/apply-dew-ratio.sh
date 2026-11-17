#!/bin/sh
# Moves the node to the Dew per XMR ratio in RATIO_FILE and adds the file's
# DEW_PRODUCTS the node lacks. The deploy runs it as root with the service
# stopped. `mesh dew-ratio` runs with the service's user and environment: it
# rescales Dew once for a raised ratio and prints the node's new MONERO_RATE_*
# and MESH_TOKEN_PRODUCTS lines, which replace the old ones in
# ENVIRONMENT_FILE. The node's current ratio and products change nothing.
#
# Usage: apply-dew-ratio.sh RATIO_FILE ENVIRONMENT_FILE BINARY DATABASE
set -eu

if [ "$#" -ne 4 ]; then
    echo "usage: apply-dew-ratio.sh RATIO_FILE ENVIRONMENT_FILE BINARY DATABASE" >&2
    exit 2
fi
ratio_file=$1
environment_file=$2
binary=$3
database=$4

. "$ratio_file"

user=$(systemctl show mesh.service -p User --value)
working_directory=$(systemctl show mesh.service -p WorkingDirectory --value)
case "$working_directory" in
    /*) ;;
    *) echo "mesh.service needs an absolute WorkingDirectory" >&2; exit 1 ;;
esac
set -- systemd-run --wait --pipe --collect --quiet \
    -p EnvironmentFile="$environment_file" \
    -p WorkingDirectory="$working_directory" \
    -p Environment=MESH_DB="$database"
if [ -n "$user" ]; then
    set -- "$@" -p User="$user"
fi
output=$("$@" "$binary" dew-ratio "$DEW_RESCALE" "$MONERO_RATE_ATOMIC_AMOUNT" "$MONERO_RATE_TOKEN_UNITS" "${DEW_PRODUCTS:-}")
printf '%s\n' "$output"

printf '%s\n' "$output" |
    grep -E '^(MONERO_RATE_ATOMIC_AMOUNT|MONERO_RATE_TOKEN_UNITS|MESH_TOKEN_PRODUCTS)=' |
    sh "$(dirname "$0")/set-environment.sh" - "$environment_file"
