#!/bin/sh
set -eu

# Docker creates new named volumes as root. Prepare the writable application
# directories, verify the unprivileged process can use them, then drop root.
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data /keys
    chown tapo:tapo /data /keys
    chown -R tapo:tapo /keys
    chmod 700 /keys

    if ! gosu tapo test -w /data || ! gosu tapo test -w /keys; then
        echo "Tapo Enhance cannot write /data or /keys; check volume ownership and permissions." >&2
        exit 1
    fi

    exec gosu tapo "$@"
fi

mkdir -p /data /keys
if [ ! -w /data ] || [ ! -w /keys ]; then
    echo "Tapo Enhance cannot write /data or /keys as uid $(id -u); check volume ownership and permissions." >&2
    exit 1
fi
exec "$@"
