#!/bin/sh
set -eu

# Docker creates a new named volume as root. Prepare only the two writable
# application directories, then run the long-lived process without privileges.
mkdir -p /data /keys
chown tapo:tapo /data /keys
chown -R tapo:tapo /keys
chmod 700 /keys

exec gosu tapo "$@"
