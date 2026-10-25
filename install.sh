#!/bin/sh
# keymouse installer -- used as-is from the tarball, and by the .deb build.
#
#   sudo ./install.sh                  install to /usr/local (default)
#   sudo ./install.sh --uninstall      remove what a previous install put down
#   ./install.sh --prefix /usr --destdir DIR   stage into DIR (packaging; no root)
#
# Installs the daemon, settings app, toggle script, systemd *user* unit,
# launcher, and the udev rule that gives the logged-in user access to the
# keyboards and /dev/uinput. It does not enable the service for anyone: each
# user turns it on from the Keyboard Mouse window ("Start automatically at
# login") or with  systemctl --user enable --now keymouse.service
set -eu

PREFIX=/usr/local
DESTDIR=
UNINSTALL=0
HERE=$(cd "$(dirname "$0")" && pwd)

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)    PREFIX=${2:?--prefix needs a directory}; shift 2 ;;
        --prefix=*)  PREFIX=${1#*=}; shift ;;
        --destdir)   DESTDIR=${2:?--destdir needs a directory}; shift 2 ;;
        --destdir=*) DESTDIR=${1#*=}; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   usage ;;
        *) echo "unknown option: $1" >&2; usage 2 >&2 ;;
    esac
done
PREFIX=${PREFIX%/}

BINDIR=$PREFIX/bin
UNITDIR=$PREFIX/lib/systemd/user
APPDIR=$PREFIX/share/applications
DOCDIR=$PREFIX/share/doc/keymouse
DATADIR=$PREFIX/share/keymouse
MANIFEST=$DATADIR/installed-files
# udev and modules-load only read /usr/lib and /etc, never /usr/local.
if [ "$PREFIX" = /usr ]; then
    UDEVDIR=/usr/lib/udev/rules.d
    MODDIR=/usr/lib/modules-load.d
else
    UDEVDIR=/etc/udev/rules.d
    MODDIR=/etc/modules-load.d
fi

need_root() {
    if [ -z "$DESTDIR" ] && [ "$(id -u)" != 0 ]; then
        echo "Run as root: sudo $0${1:+ $1}  (or use --destdir to stage)" >&2
        exit 1
    fi
}

reload_udev() {
    [ -z "$DESTDIR" ] || return 0
    command -v udevadm >/dev/null || return 0
    udevadm control --reload-rules || true
    # "change" re-applies the uaccess tag, so the ACLs appear at once for the
    # user already logged in -- no reboot needed.
    udevadm trigger --action=change --subsystem-match=input --subsystem-match=misc || true
}

if [ "$UNINSTALL" = 1 ]; then
    need_root --uninstall
    if [ ! -f "$DESTDIR$MANIFEST" ]; then
        echo "No keymouse install found under $PREFIX ($MANIFEST missing)." >&2
        exit 1
    fi
    while IFS= read -r f; do
        rm -f "$DESTDIR$f"
    done < "$DESTDIR$MANIFEST"
    rm -f "$DESTDIR$MANIFEST"
    rmdir "$DESTDIR$DATADIR" "$DESTDIR$DOCDIR" 2>/dev/null || true
    reload_udev
    echo "keymouse removed. Running copies stop at logout, or now with:"
    echo "  systemctl --user disable --now keymouse.service"
    echo "Settings in ~/.config/keymouse.conf were left alone."
    exit 0
fi

need_root
INSTALLED=""

put() {     # put MODE SOURCE DEST [substitute]
    mode=$1 src=$2 dest=$3
    mkdir -p "$DESTDIR$(dirname "$dest")"
    if [ "${4:-}" = subst ]; then
        sed -e "s|@BINDIR@|$BINDIR|g" -e "s|@DOCDIR@|$DOCDIR|g" \
            -e '1s|^#!/usr/bin/env python3$|#!/usr/bin/python3|' \
            "$HERE/$src" > "$DESTDIR$dest"
        chmod "$mode" "$DESTDIR$dest"
    else
        install -m "$mode" "$HERE/$src" "$DESTDIR$dest"
    fi
    INSTALLED="$INSTALLED$dest
"
}

put 0755 src/keymouse.py             "$BINDIR/keymouse"            subst
put 0755 src/keymouse-control.py     "$BINDIR/keymouse-control"    subst
put 0755 src/keymouse-toggle.sh      "$BINDIR/keymouse-toggle"
put 0644 data/keymouse.service.in    "$UNITDIR/keymouse.service"   subst
put 0644 data/keymouse-control.desktop.in "$APPDIR/keymouse-control.desktop" subst
put 0644 data/70-keymouse.rules      "$UDEVDIR/70-keymouse.rules"
put 0644 data/keymouse-modules.conf  "$MODDIR/keymouse.conf"
put 0644 README.md                   "$DOCDIR/README.md"
put 0644 data/keymouse.conf.example  "$DOCDIR/keymouse.conf.example"

mkdir -p "$DESTDIR$DATADIR"
printf '%s' "$INSTALLED" > "$DESTDIR$MANIFEST"

[ -n "$DESTDIR" ] && exit 0

# ---- live install only ----------------------------------------------------
modprobe uinput 2>/dev/null || true
reload_udev
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPDIR" || true

missing=""
/usr/bin/python3 -c 'import evdev' 2>/dev/null || missing="$missing evdev"
/usr/bin/python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || missing="$missing PyQt6"
command -v busctl >/dev/null || missing="$missing busctl(systemd)"

echo "keymouse installed to $PREFIX."
if [ -n "$missing" ]; then
    echo
    echo "MISSING:$missing -- install them first:"
    echo "  Debian/Ubuntu: sudo apt install python3-evdev python3-pyqt6"
    echo "  Fedora:        sudo dnf install python3-evdev python3-pyqt6"
    echo "  Arch:          sudo pacman -S python-evdev python-pyqt6"
fi
echo
echo "Next, as your normal user (not root):"
echo "  open \"Keyboard Mouse\" from the application menu and press Start"
echo "  (tick \"Start automatically at login\" to keep it), or run"
echo "  systemctl --user daemon-reload && systemctl --user enable --now keymouse.service"
