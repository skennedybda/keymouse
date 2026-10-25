#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Scott Kennedy
# keymouse-toggle.sh [toggle|off|stop] -- for launchers and KDE shortcuts.
# (Inside a Citrix window KDE shortcuts may not fire; the keyboard gesture,
#  double-tap Right Shift by default, always does.)
UNIT=keymouse.service
case "${1:-toggle}" in
    toggle)
        if ! systemctl --user is-active -q "$UNIT"; then
            systemctl --user start "$UNIT" || exit 1
            sleep 1.5
        fi
        systemctl --user kill -s USR1 "$UNIT" ;;
    off)  systemctl --user kill -s USR2 "$UNIT" ;;
    stop) systemctl --user stop "$UNIT" ;;
    *) echo "usage: $0 [toggle|off|stop]" >&2; exit 2 ;;
esac
