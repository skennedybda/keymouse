#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Scott Kennedy
# Build the release artifacts into dist/:
#   keymouse-VERSION.tar.gz   source tarball (extract, then: sudo ./install.sh)
#   keymouse_VERSION_all.deb  Debian/Ubuntu package
# Runs the test suite first; set SKIP_TESTS=1 to skip it.
# MAINTAINER and HOMEPAGE override the .deb metadata.
set -eu
umask 022
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/keymouse.py)
MAINTAINER=${MAINTAINER:-"Scott Kennedy <67512861+skennedybda@users.noreply.github.com>"}
HOMEPAGE=${HOMEPAGE:-"https://github.com/skennedybda/keymouse"}
[ -n "$VERSION" ] || { echo "no __version__ in src/keymouse.py" >&2; exit 1; }
grep -q "^VERSION = \"$VERSION\"" src/keymouse-control.py || {
    echo "version mismatch: keymouse-control.py is not $VERSION" >&2; exit 1; }

if [ "${SKIP_TESTS:-0}" != 1 ]; then
    QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -q
fi

rm -rf build dist
mkdir -p build dist

# ---- tarball --------------------------------------------------------------
NAME=keymouse-$VERSION
mkdir -p "build/$NAME"
cp -r README.md LICENSE install.sh src data tests "build/$NAME/"
find "build/$NAME" -name __pycache__ -prune -exec rm -rf {} +
tar -C build --owner=0 --group=0 --sort=name -czf "dist/$NAME.tar.gz" "$NAME"

# ---- .deb -----------------------------------------------------------------
PKG=build/deb
./install.sh --prefix /usr --destdir "$PKG" >/dev/null
rm "$PKG/usr/share/keymouse/installed-files"   # dpkg keeps its own list
rmdir "$PKG/usr/share/keymouse"
mkdir -p "$PKG/DEBIAN"
SIZE=$(du -sk "$PKG/usr" | cut -f1)
sed -e "s|@VERSION@|$VERSION|" -e "s|@MAINTAINER@|$MAINTAINER|" \
    -e "s|@HOMEPAGE@|$HOMEPAGE|" -e "s|@SIZE@|$SIZE|" \
    packaging/debian/control.in > "$PKG/DEBIAN/control"
install -m 0755 packaging/debian/postinst packaging/debian/postrm "$PKG/DEBIAN/"
DOC=$PKG/usr/share/doc/keymouse
cat > "$DOC/copyright" <<COPYRIGHT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: keymouse
Source: $HOMEPAGE

Files: *
Copyright: 2026 Scott Kennedy
License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify it
 under the terms of the GNU General Public License as published by the Free
 Software Foundation, either version 3 of the License, or (at your option)
 any later version.
 .
 On Debian systems, the full text of the GNU General Public License
 version 3 can be found in /usr/share/common-licenses/GPL-3.
COPYRIGHT
chmod 0644 "$DOC/copyright"
rm -f "$DOC/LICENSE"      # the full text is already on every Debian system
printf 'keymouse (%s) unstable; urgency=low\n\n  * Release %s.\n\n -- %s  %s\n' \
    "$VERSION" "$VERSION" "$MAINTAINER" "$(date -R)" | gzip -9n > "$DOC/changelog.Debian.gz"
(cd "$PKG" && find usr -type f -exec md5sum {} + | sort -k2) > "$PKG/DEBIAN/md5sums"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "dist/keymouse_${VERSION}_all.deb" >/dev/null

(cd dist && sha256sum -- * > SHA256SUMS)
echo "built:"; ls -l dist
