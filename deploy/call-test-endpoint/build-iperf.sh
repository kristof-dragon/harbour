#!/bin/sh
# Shared by both images; verified upstream release archive, no runtime compiler.
set -eu
version=3.22
checksum=1c0d0fb02c52626111d6e132db80edfbf27bbaff8bd9245df2a371dcb0b35a92
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cd "$work"
curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    "https://downloads.es.net/pub/iperf/iperf-$version.tar.gz" -o iperf.tar.gz
printf '%s  iperf.tar.gz\n' "$checksum" | sha256sum --check --strict
tar -xzf iperf.tar.gz
cd "iperf-$version"
# Harbour uses source-restricted endpoints, not iperf's optional RSA authentication.
./configure --prefix=/usr/local --disable-shared --without-openssl
make -j2
make install
/usr/local/bin/iperf3 --version
