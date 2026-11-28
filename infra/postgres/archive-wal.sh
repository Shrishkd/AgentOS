#!/bin/sh
set -eu
umask 077
target=/archive/$2
if [ -e "$target" ]; then
    cmp -s "$1" "$target"
    exit $?
fi
partial=$(mktemp /archive/.incoming.XXXXXX)
trap 'rm -f "$partial"' EXIT
cp "$1" "$partial"
sync "$partial"
ln "$partial" "$target"
sync /archive
