#!/bin/sh
# Fetch and unpack aria2 + its runtime libraries into .aria2root/ without root.
#
# Prefer your package manager (`apt install aria2`, `dnf install aria2`, ...).
# This script is a fallback for environments where installing packages is not
# possible; it uses `apt-get download` + `dpkg-deb -x`, which need no privileges.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
deb="$root/.debcache"
out="$root/.aria2root"

mkdir -p "$deb" "$out"
cd "$deb"

pkgs="aria2 libaria2-0 libcares2 libgnutls30t64 libnettle8t64 libsqlite3-0 \
libssh2-1t64 libxml2 libgmp10 zlib1g"

# shellcheck disable=SC2086
apt-get download $pkgs

for d in "$deb"/*.deb; do
    dpkg-deb -x "$d" "$out"
done

echo "aria2c unpacked into $out"
"$root/tools/aria2c" --version | head -1
