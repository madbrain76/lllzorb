#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

if [ "$#" -gt 1 ]; then
    echo "Usage: $0 [PREFIX] (default: /usr/local)" >&2
    exit 2
fi
case "${1-}" in
    -h|--help)
        echo "Usage: $0 [PREFIX] (default: /usr/local)"
        exit 0
        ;;
esac
prefix=${1:-/usr/local}
case "$prefix" in
    /*) ;;
    *) echo "PREFIX must be an absolute path" >&2; exit 2 ;;
esac
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -d "$prefix/bin" "$prefix/share/doc/lllzorb"
install -m 755 "$source_dir/lllzorb" "$prefix/bin/lllzorb"
install -m 644 "$source_dir/README.txt" "$source_dir/LICENSE" "$prefix/share/doc/lllzorb/"
echo "Installed lllzorb to $prefix/bin/lllzorb"
